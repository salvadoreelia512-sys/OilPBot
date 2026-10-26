// Netlify runs Node functions; host the Python ASGI service separately.
export async function handler(event) {
    const json = (statusCode, detail) => ({
        statusCode,
        headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
        body: JSON.stringify({ detail }),
    });
    let path = event.path.replace(/^\/\.netlify\/functions\/api/, '/api');
    if (path === '/predict' || path === '/health') path = '/api' + path;
    const routes = { '/api/strategy': 'GET', '/api/news': 'GET', '/api/historical': 'GET', '/api/statistics': 'GET', '/api/market': 'GET', '/api/forecast': 'POST', '/api/predict': 'POST', '/api/quote': 'GET', '/api/health': 'GET' };
    if (!routes[path]) return json(404, 'Unknown API route.');
    if (event.httpMethod !== routes[path]) return json(405, 'Method not allowed.');
    const base = process.env.OILPBOT_API_URL;
    if (!base) return json(503, 'Backend is not configured. Set OILPBOT_API_URL in Netlify to your deployed FastAPI service URL.');
    let url;
    try {
        url = new URL(base);
        if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) throw new Error();
    } catch {
        return json(503, 'OILPBOT_API_URL must be an HTTPS origin without a path or credentials.');
    }
    try {
        const target = new URL(path, url);
        if (event.queryStringParameters?.data_mode) target.searchParams.set('data_mode', event.queryStringParameters.data_mode);
        const response = await fetch(target, {
            method: event.httpMethod,
            headers: { 'content-type': 'application/json' },
            body: event.httpMethod === 'POST' ? (event.isBase64Encoded ? Buffer.from(event.body || '', 'base64').toString() : event.body) : undefined,
            signal: AbortSignal.timeout(25000),
            redirect: 'error',
        });
        if (!(response.headers.get('content-type') || '').includes('application/json')) {
            return json(502, 'Backend returned a non-JSON response. Check OILPBOT_API_URL and backend logs.');
        }
        const body = await response.text();
        JSON.parse(body);
        return { statusCode: response.status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' }, body };
    } catch {
        return json(502, 'Backend is unreachable or returned invalid JSON. Check the FastAPI service and retry.');
    }
}
