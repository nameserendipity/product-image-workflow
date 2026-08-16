export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, options);
  const body = (await response.json()) as T & { error?: string };
  if (!response.ok) {
    throw new Error(body.error || '请求失败，请稍后重试。');
  }
  return body;
}

export function jsonRequest(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}
