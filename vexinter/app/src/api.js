// La credencial vive en una cookie HttpOnly emitida por /v1/auth/login:
// el JS nunca ve el token, solo un flag de UI en sessionStorage.
const FLAG = 'vex_auth';

export const isAuthed = () => sessionStorage.getItem(FLAG) === '1';
const markAuthed = () => sessionStorage.setItem(FLAG, '1');
export const clearAuthed = () => sessionStorage.removeItem(FLAG);

export async function checkSession() {
  try {
    const r = await fetch('/v1/auth/session', { credentials: 'same-origin' });
    const d = await r.json();
    if (d.authenticated) markAuthed(); else clearAuthed();
    return !!d.authenticated;
  } catch {
    clearAuthed();
    return false;
  }
}

export async function login(token) {
  const r = await fetch('/v1/auth/login', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!r.ok) {
    let d = {}; try { d = await r.json(); } catch { /* sin cuerpo */ }
    throw Object.assign(new Error(d.detail || `HTTP ${r.status}`),
                        { status: r.status });
  }
  markAuthed();
}

export async function logout() {
  try {
    await fetch('/v1/auth/logout', {
      method: 'DELETE', credentials: 'same-origin',
    });
  } finally {
    clearAuthed();
  }
}

export async function api(path, { method = 'GET', body, query } = {}) {
  const url = query ? path + '?' + new URLSearchParams(query) : path;
  const r = await fetch(url, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 || r.status === 403) {
    clearAuthed();
    throw Object.assign(new Error('unauthorized'), { auth: true });
  }
  if (!r.ok) {
    let d = {}; try { d = await r.json(); } catch { /* sin cuerpo */ }
    throw Object.assign(new Error(d.detail || `HTTP ${r.status}`),
                        { status: r.status, detail: d });
  }
  return r.status === 204 ? null : r.json();
}
