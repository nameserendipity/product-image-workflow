import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const inputPath = process.argv[2];
if (!inputPath) throw new Error("Missing export payload path");

const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const outputPath = path.resolve(payload.output_path);
const outputDir = path.dirname(outputPath);

const workbook = Workbook.create();
const sheets = {
  overview: workbook.worksheets.add("总览"),
  main: workbook.worksheets.add("主图"),
  detail: workbook.worksheets.add("详情图"),
  sku: workbook.worksheets.add("SKU"),
  parameters: workbook.worksheets.add("商品参数"),
  title: workbook.worksheets.add("标题"),
  video: workbook.worksheets.add("视频"),
};

const colors = {
  header: "#17324D",
  border: "#D7DEE5",
  link: "#0563C1",
};
let tableCounter = 0;

function localLink(value) {
  const text = String(value ?? "");
  if (!text) return "";
  if (/^https?:\/\//i.test(text)) return text;
  const relative = path.relative(outputDir, text).replaceAll(path.sep, "/");
  return relative || path.basename(text);
}

function imageLink(publicUrl, localPath) {
  const remote = String(publicUrl ?? "");
  return remote || localLink(localPath);
}

function columnNumber(column) {
  return [...String(column)].reduce((value, character) => value * 26 + character.charCodeAt(0) - 64, 0);
}

function textFormula(value) {
  const text = String(value ?? "");
  return text ? `="${text.replaceAll('"', '""')}"` : "";
}

function asNumberOrText(value) {
  if (typeof value === "number") return value;
  const text = String(value ?? "").trim();
  if (!text) return "";
  const numeric = Number(text.replace(/[￥$,]/g, ""));
  return Number.isFinite(numeric) ? numeric : text;
}

function textUnits(value) {
  return [...String(value ?? "")].reduce((total, character) => total + (character.charCodeAt(0) > 127 ? 2 : 1), 0);
}

function applyDynamicRowHeights(sheet, rows, widths, options = {}) {
  const minimum = options.minimum ?? 22;
  const maximum = options.maximum ?? 96;
  for (let rowIndex = 1; rowIndex < rows.length; rowIndex += 1) {
    let lines = 1;
    for (let columnIndex = 0; columnIndex < rows[rowIndex].length; columnIndex += 1) {
      const column = String.fromCharCode(65 + columnIndex);
      const capacity = Math.max(Number(widths[column] || 16), 8);
      lines = Math.max(lines, Math.ceil(textUnits(rows[rowIndex][columnIndex]) / capacity));
    }
    sheet.getRange(`A${rowIndex + 1}:A${rowIndex + 1}`).format.rowHeight = Math.min(maximum, Math.max(minimum, 18 * lines + 4));
  }
}

function applyTableStyle(sheet, lastRow, lastCol, widths = {}) {
  const address = `A1:${lastCol}${Math.max(lastRow, 1)}`;
  const range = sheet.getRange(address);
  range.format = {
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: colors.border },
  };
  const header = sheet.getRange(`A1:${lastCol}1`);
  header.format = {
    fill: colors.header,
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
  };
  header.format.rowHeight = 26;
  sheet.freezePanes.freezeRows(1);
  for (const [column, width] of Object.entries(widths)) {
    sheet.getRange(`${column}:${column}`).format.columnWidth = width;
  }
  if (lastRow >= 2) sheet.getRange(`A2:${lastCol}${lastRow}`).format.rowHeight = 22;
  try {
    tableCounter += 1;
    sheet.tables.add(address, true, `ExportTable${tableCounter}`);
  } catch {
    // A table is optional for an export; styling and values remain intact.
  }
}

async function addThumbnail(sheet, row, column, value) {
  const imagePath = String(value ?? "");
  if (!imagePath) return;
  try {
    const bytes = await fs.readFile(imagePath);
    const ext = path.extname(imagePath).toLowerCase();
    const mime = ext === ".jpg" || ext === ".jpeg" ? "image/jpeg" : ext === ".webp" ? "image/webp" : "image/png";
    const dataUrl = `data:${mime};base64,${bytes.toString("base64")}`;
    const anchorColumn = columnNumber(column);
    sheet.images.add({
      dataUrl,
      anchor: { from: { row: row - 1, col: anchorColumn - 1 }, extent: { widthPx: 120, heightPx: 96 } },
    });
    sheet.getRange(`${column}${row}`).format.rowHeight = 76;
  } catch {
    // A missing preview must not discard the path or status.
  }
}

async function writeImageSheet(sheet, records) {
  const rows = [["序号", "采集图缩略图", "采集图路径", "生成图缩略图", "生成图路径", "生成状态"]];
  for (const [index, record] of records.entries()) {
    rows.push([
      index + 1,
      "",
      imageLink(record.source_public_url, record.source_path),
      "",
      imageLink(record.output_public_url, record.output_path),
      record.generation_status || "未生成",
    ]);
  }
  const lastRow = Math.max(rows.length, 1);
  sheet.getRange(`A1:F${lastRow}`).values = rows;
  if (lastRow >= 2) {
    sheet.getRange(`C2:C${lastRow}`).format.font = { color: colors.link };
    sheet.getRange(`E2:E${lastRow}`).format.font = { color: colors.link };
  }
  applyTableStyle(sheet, lastRow, "F", { A: 10, B: 16, C: 72, D: 16, E: 72, F: 16 });
  for (const [index, record] of records.entries()) {
    await addThumbnail(sheet, index + 2, "B", record.source_path);
    await addThumbnail(sheet, index + 2, "D", record.output_path);
  }
}

async function writeSkuSheet(sheet, rows) {
  const headers = ["序号", "商品ID", "SKU标签", "规格", "颜色", "价格", "解析状态", "采集图缩略图", "采集图路径", "生成图缩略图", "生成图路径", "生成图状态"];
  const values = [headers];
  const formulas = [headers.map(() => "")];
  for (const [index, row] of rows.entries()) {
    values.push([
      index + 1,
      "",
      row.sku_label || "",
      row.spec_text || "",
      row.color_text || "",
      asNumberOrText(row.price),
      row.parse_status || "",
      "",
      imageLink(row.source_public_url, row.source_path),
      "",
      imageLink(row.output_public_url, row.output_path),
      row.generation_status || "未生成",
    ]);
    formulas.push(["", textFormula(row.product_id), "", "", "", "", "", "", "", "", "", ""]);
  }
  const lastRow = Math.max(values.length, 1);
  sheet.getRange(`A1:L${lastRow}`).values = values;
  sheet.getRange(`A1:L${lastRow}`).formulas = formulas;
  if (lastRow >= 2) {
    sheet.getRange(`I2:I${lastRow}`).format.font = { color: colors.link };
    sheet.getRange(`K2:K${lastRow}`).format.font = { color: colors.link };
  }
  applyTableStyle(sheet, lastRow, "L", { A: 8, B: 18, C: 34, D: 18, E: 16, F: 14, G: 16, H: 16, I: 72, J: 16, K: 72, L: 16 });
  for (const [index, row] of rows.entries()) {
    await addThumbnail(sheet, index + 2, "H", row.source_path);
    await addThumbnail(sheet, index + 2, "J", row.output_path);
  }
}

function writeParameterSheet(sheet, parameters) {
  const rows = [["类型", "参数名", "参数值", "处理方式"]];
  for (const item of parameters) rows.push([item.type || "商品参数", item.name || "", item.value || "", item.handling || "采集原值"]);
  sheet.getRange(`A1:D${Math.max(rows.length, 1)}`).values = rows;
  const widths = { A: 16, B: 30, C: 72, D: 22 };
  applyTableStyle(sheet, rows.length, "D", widths);
  applyDynamicRowHeights(sheet, rows, widths);
}

function writeTitleSheet(sheet, title) {
  const rows = [["序号", "长标题", "短标题"], [1, title.long_title || "", title.short_title || ""]];
  const widths = { A: 10, B: 92, C: 48 };
  sheet.getRange("A1:C2").values = rows;
  applyTableStyle(sheet, 2, "C", widths);
  applyDynamicRowHeights(sheet, rows, widths);
}

function writeVideoSheet(sheet, videos) {
  const rows = [["序号", "视频名称", "公网播放地址", "访问说明"]];
  for (const [index, video] of videos.entries()) {
    rows.push([index + 1, video.name || "商品主视频", String(video.url || ""), video.note || "复制完整地址到浏览器即可播放；链接可能含临时授权参数。"]);
  }
  if (rows.length === 1) rows.push([1, "商品主视频", "", "未找到可打开的视频 URL"]);
  sheet.getRange(`A1:D${rows.length}`).values = rows;
  if (rows.length >= 2) sheet.getRange(`C2:C${rows.length}`).format.font = { color: colors.link };
  const widths = { A: 10, B: 30, C: 104, D: 58 };
  applyTableStyle(sheet, rows.length, "D", widths);
  applyDynamicRowHeights(sheet, rows, widths, { maximum: 76 });
}

function writeOverview(sheet, rows) {
  const formulas = rows.map(() => ["", ""]);
  rows.forEach((row, index) => {
    if (String(row[0] || "").endsWith("商品ID")) {
      formulas[index][1] = textFormula(row[1]);
      row[1] = "";
    }
  });
  sheet.getRange(`A1:B${rows.length}`).values = rows;
  sheet.getRange(`A1:B${rows.length}`).formulas = formulas;
  const widths = { A: 28, B: 94 };
  applyTableStyle(sheet, rows.length, "B", widths);
  applyDynamicRowHeights(sheet, rows, widths, { maximum: 76 });
}

writeOverview(sheets.overview, payload.overview);
await writeImageSheet(sheets.main, payload.main || []);
await writeImageSheet(sheets.detail, payload.detail || []);
await writeSkuSheet(sheets.sku, payload.sku || []);
writeParameterSheet(sheets.parameters, payload.parameters || []);
writeTitleSheet(sheets.title, payload.title || {});
writeVideoSheet(sheets.video, payload.videos || []);

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(outputPath);
