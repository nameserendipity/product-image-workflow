from __future__ import annotations

import queue
import threading
from datetime import datetime
from pathlib import Path
from tkinter import END, StringVar, Tk, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from image_workflows import ApiSettings, WorkflowRunner, load_manifest_tasks


CATEGORY_LABELS = {"main": "主图", "sku": "SKU 图", "detail": "详情图"}
STATUS_LABELS = {
    "pending": "等待处理",
    "analyzing": "正在分析",
    "generating": "正在生成",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

class WorkflowApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("商品图片生成工作流")
        self.root.minsize(980, 700)
        self.events: queue.Queue[dict] = queue.Queue()
        self.runner: WorkflowRunner | None = None
        self.task_rows: dict[tuple[str, int], str] = {}

        self.manifest_var = StringVar()
        self.product_var = StringVar()
        self.output_var = StringVar()
        self.base_url_var = StringVar(value="https://api.humanwill.xyz")
        self.vision_key_var = StringVar()
        self.image_key_var = StringVar()
        self.concurrency_var = StringVar(value="2")

        self._build_form()
        self._build_tasks()
        self._build_log()
        self.root.after(150, self._drain_events)

    def _build_form(self) -> None:
        frame = ttk.LabelFrame(self.root, text="任务设置")
        frame.pack(fill="x", padx=12, pady=10)
        fields = [
            ("采集清单", self.manifest_var, self._select_manifest),
            ("我方产品图", self.product_var, self._select_product),
            ("生成输出目录", self.output_var, self._select_output),
        ]
        for row, (label, variable, command) in enumerate(fields):
            ttk.Label(frame, text=label, width=14).grid(row=row, column=0, padx=8, pady=5, sticky="w")
            ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, padx=8, pady=5, sticky="ew")
            ttk.Button(frame, text="浏览", command=command).grid(row=row, column=2, padx=8, pady=5)

        ttk.Label(frame, text="中转基础地址", width=14).grid(row=0, column=3, padx=8, pady=5, sticky="w")
        ttk.Entry(frame, textvariable=self.base_url_var, width=34).grid(row=0, column=4, padx=8, pady=5, sticky="ew")
        ttk.Label(frame, text="视觉模型 API Key", width=14).grid(row=1, column=3, padx=8, pady=5, sticky="w")
        ttk.Entry(frame, textvariable=self.vision_key_var, show="*", width=34).grid(row=1, column=4, padx=8, pady=5, sticky="ew")
        ttk.Label(frame, text="生图模型 API Key", width=14).grid(row=2, column=3, padx=8, pady=5, sticky="w")
        ttk.Entry(frame, textvariable=self.image_key_var, show="*", width=34).grid(row=2, column=4, padx=8, pady=5, sticky="ew")
        ttk.Label(frame, text="并发任务数", width=14).grid(row=3, column=0, padx=8, pady=5, sticky="w")
        ttk.Spinbox(frame, from_=1, to=3, textvariable=self.concurrency_var, width=6).grid(
            row=3, column=1, padx=8, pady=5, sticky="w"
        )
        self.start_button = ttk.Button(frame, text="开始生成", command=self._start)
        self.start_button.grid(row=3, column=3, padx=8, pady=5, sticky="w")
        ttk.Button(frame, text="取消任务", command=self._cancel).grid(row=3, column=4, padx=8, pady=5, sticky="w")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(4, weight=1)

    def _build_tasks(self) -> None:
        frame = ttk.LabelFrame(self.root, text="主图 / SKU 图 / 详情图工作流")
        frame.pack(fill="both", expand=True, padx=12, pady=0)
        columns = ("category", "index", "status", "source", "output")
        self.tasks = ttk.Treeview(frame, columns=columns, show="headings", height=16)
        headings = {"category": "类型", "index": "序号", "status": "状态", "source": "来源图片", "output": "生成文件"}
        widths = {"category": 90, "index": 55, "status": 100, "source": 330, "output": 330}
        for column in columns:
            self.tasks.heading(column, text=headings[column])
            self.tasks.column(column, width=widths[column], anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tasks.yview)
        self.tasks.configure(yscrollcommand=scroll.set)
        self.tasks.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self.root, text="执行进度")
        frame.pack(fill="both", padx=12, pady=10)
        self.log = ScrolledText(frame, height=8, state="disabled")
        self.log.pack(fill="both", expand=True)

    def _select_manifest(self) -> None:
        selected = filedialog.askopenfilename(filetypes=[("采集清单", "manifest.json"), ("JSON 文件", "*.json")])
        if not selected:
            return
        self.manifest_var.set(selected)
        manifest_path = Path(selected)
        self.output_var.set(str(manifest_path.parent / f"生成结果-{datetime.now():%Y%m%d-%H%M%S}"))
        self._load_task_rows(manifest_path)

    def _select_product(self) -> None:
        selected = filedialog.askopenfilename(filetypes=[("图片文件", "*.jpg *.jpeg *.png *.webp")])
        if selected:
            self.product_var.set(selected)

    def _select_output(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.output_var.set(selected)

    def _load_task_rows(self, manifest_path: Path) -> None:
        for row in self.tasks.get_children():
            self.tasks.delete(row)
        self.task_rows.clear()
        try:
            tasks = load_manifest_tasks(manifest_path)
        except Exception as error:
            messagebox.showerror("采集清单", str(error))
            return
        for task in tasks:
            row = self.tasks.insert("", END, values=(CATEGORY_LABELS[task.category], task.ordinal, "等待处理", task.source_path, ""))
            self.task_rows[(task.category, task.ordinal)] = row
        self._append_log(f"已加载 {len(tasks)} 张来源图片。")

    def _start(self) -> None:
        manifest = Path(self.manifest_var.get().strip())
        product = Path(self.product_var.get().strip())
        output = Path(self.output_var.get().strip())
        if not manifest.is_file() or not product.is_file() or not str(output):
            messagebox.showerror("任务设置", "请选择采集清单、我方产品图和生成输出目录。")
            return
        if not self.vision_key_var.get().strip() or not self.image_key_var.get().strip():
            messagebox.showerror("任务设置", "请填写本次会话使用的两把 API Key。")
            return
        try:
            concurrency = int(self.concurrency_var.get())
        except ValueError:
            messagebox.showerror("任务设置", "并发任务数只能填写 1、2 或 3。")
            return

        self._load_task_rows(manifest)
        settings = ApiSettings(
            base_url=self.base_url_var.get().strip(),
            vision_api_key=self.vision_key_var.get().strip(),
            image_api_key=self.image_key_var.get().strip(),
        )
        self.runner = WorkflowRunner(settings, callback=lambda event: self.events.put({"kind": "task", **event}))
        self.start_button.configure(state="disabled")
        thread = threading.Thread(
            target=self._run_workflows,
            args=(manifest, product, output, concurrency),
            daemon=True,
        )
        thread.start()

    def _run_workflows(self, manifest: Path, product: Path, output: Path, concurrency: int) -> None:
        try:
            records = self.runner.run(manifest, product, output, concurrency) if self.runner else []
            completed = sum(record.get("status") == "completed" for record in records)
            failed = sum(record.get("status") == "failed" for record in records)
            self.events.put({"kind": "finished", "completed": completed, "failed": failed, "output": str(output)})
        except Exception as error:
            self.events.put({"kind": "error", "error": str(error)})

    def _cancel(self) -> None:
        if self.runner:
            self.runner.cancel()
            self._append_log("已请求取消。正在调用中的接口完成后会停止后续任务。")

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event["kind"] == "task":
                key = (event["category"], event["ordinal"])
                row = self.task_rows.get(key)
                if row:
                    current = list(self.tasks.item(row, "values"))
                    current[2] = STATUS_LABELS.get(event["status"], event["status"])
                    if event.get("output_path"):
                        current[4] = event["output_path"]
                    self.tasks.item(row, values=current)
                suffix = event.get("error") or event.get("output_path") or ""
                category = CATEGORY_LABELS.get(event["category"], event["category"])
                status = STATUS_LABELS.get(event["status"], event["status"])
                self._append_log(f"{category} #{event['ordinal']}：{status} {suffix}")
            elif event["kind"] == "finished":
                self.start_button.configure(state="normal")
                self._append_log(
                    f"任务结束：完成 {event['completed']}，失败 {event['failed']}，输出目录：{event['output']}"
                )
            elif event["kind"] == "error":
                self.start_button.configure(state="normal")
                self._append_log(f"工作流错误：{event['error']}")
                messagebox.showerror("工作流错误", event["error"])
        self.root.after(150, self._drain_events)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(END, message + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")


if __name__ == "__main__":
    window = Tk()
    WorkflowApp(window)
    window.mainloop()
