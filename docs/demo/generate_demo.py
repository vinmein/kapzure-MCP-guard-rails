"""Run both gateways, then render their recorded decisions as a looping GIF.

Requires the Python project's dependencies, Pillow, and `npm ci` in javascript/.
Run from any directory: python3 docs/demo/generate_demo.py
"""

import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
W, H = 1200, 760
FRAMES_PER_SCENE = 75
FRAME_MS = 70
BG, PANEL, BORDER = '#09111f', '#111e30', '#293b53'
WHITE, MUTED, GREEN, RED, AMBER, BLUE = '#eef5ff', '#91a5bf', '#56e3b0', '#ff7488', '#ffc36c', '#77b6ff'


def collect():
    sys.path.insert(0, str(ROOT / 'python'))
    sys.path.insert(0, str(ROOT / 'python/src'))
    import httpx
    from fastapi.testclient import TestClient
    from examples.demo_server import app as demo
    from mcp_guard.app import create_app
    from mcp_guard.config import Settings

    policy = json.loads((ROOT / 'python/policy.json').read_text())
    policy['tools']['echo']['rate_limit'] = {'requests': 1, 'window_seconds': 60}
    token = 'local-animation-demo-token-' + 'x' * 40
    env_name = policy['clients']['demo-client']['token_env']
    previous = os.environ.get(env_name)
    os.environ[env_name] = token
    calls = []
    scenarios = [
        {'title': 'A valid call goes through', 'name': 'echo', 'arguments': {'text': 'hello'}, 'status': 200},
        {'title': 'A forbidden tool stops here', 'name': 'admin_reset', 'arguments': {}, 'status': 403},
        {'title': 'Invalid input never reaches the tool', 'name': 'echo', 'arguments': {'text': 42}, 'status': 400},
        {'title': 'Repeated calls hit the rate limit', 'name': 'echo', 'arguments': {'text': 'hello again'}, 'status': 429},
    ]

    class CountingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request)
            return await httpx.ASGITransport(app=demo).handle_async_request(request)

    python_results = []
    try:
        with TestClient(create_app(Settings.model_validate(policy), CountingTransport())) as client:
            for i, scenario in enumerate(scenarios):
                before = len(calls)
                response = client.post('/mcp', headers={'Authorization': f'Bearer {token}',
                    'MCP-Protocol-Version': '2025-11-25'}, json={
                    'jsonrpc': '2.0', 'id': i + 1, 'method': 'tools/call',
                    'params': {'name': scenario['name'], 'arguments': scenario['arguments']}})
                python_results.append({'status': response.status_code, 'body': response.json(),
                                       'forwarded': len(calls) > before})
    finally:
        if previous is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = previous

    code = """
import { createApp } from './src/app.js';
import { createDemo } from './examples/demo-server.js';
const input = JSON.parse(await new Promise(resolve => {
  let data = ''; process.stdin.on('data', chunk => data += chunk);
  process.stdin.on('end', () => resolve(data));
}));
let calls = 0;
const demo = createDemo();
const app = createApp(input.policy, { env: { MCP_GUARD_DEMO_TOKEN: input.token }, audit: () => {},
  transport: async (_, options) => {
    calls++;
    const response = await demo.inject({ method: 'POST', url: '/mcp', headers: options.headers, payload: options.body });
    return { status: response.statusCode, headers: response.headers, body: response.rawPayload };
  }
});
const results = [];
try {
  for (const [i, scenario] of input.scenarios.entries()) {
    const before = calls;
    const response = await app.inject({ method: 'POST', url: '/mcp',
      headers: { authorization: `Bearer ${input.token}`, 'mcp-protocol-version': '2025-11-25' },
      payload: { jsonrpc: '2.0', id: i + 1, method: 'tools/call', params: { name: scenario.name, arguments: scenario.arguments } }
    });
    results.push({ status: response.statusCode, body: response.json(), forwarded: calls > before });
  }
  console.log(JSON.stringify(results));
} finally { await app.close(); await demo.close(); }
"""
    completed = subprocess.run(['node', '--input-type=module', '-e', code],
        cwd=ROOT / 'javascript', input=json.dumps({'policy': policy, 'token': token, 'scenarios': scenarios}),
        text=True, capture_output=True, check=True)
    js_results = json.loads(completed.stdout)
    for i, scenario in enumerate(scenarios):
        py, js = python_results[i], js_results[i]
        assert py['status'] == js['status'] == scenario['status']
        assert py['forwarded'] == js['forwarded'] == (i == 0)
        scenario['python'] = py
        scenario['javascript'] = js
    (OUT / 'recorded-responses.json').write_text(json.dumps({
        'source': 'Actual in-process gateway requests; animation replays recorded outcomes.',
        'demo_policy': 'One echo call per client per 60 seconds; other policies unchanged.',
        'scenarios': scenarios}, indent=2) + '\n')
    return scenarios


def font(size, bold=False, mono=False):
    candidates = ([
        '/System/Library/Fonts/Menlo.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
    ] if mono else [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    ])
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


F = {key: font(size, bold, mono) for key, size, bold, mono in [
    ('title', 38, True, False), ('subtitle', 18, False, False), ('label', 14, True, False),
    ('node', 23, True, False), ('small', 15, False, False), ('mono', 16, False, True),
    ('decision', 31, True, False), ('status', 20, True, True),
]}


def render(index, phase, scenario):
    image = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(image)
    def text(x, y, value, key='small', fill=WHITE):
        draw.text((x, y), value, font=F[key], fill=fill)
    def box(rect, fill=PANEL, outline=BORDER, radius=18, width=1):
        draw.rounded_rectangle(rect, radius, fill=fill, outline=outline, width=width)
    def centered(x, y, value, key='small', fill=WHITE):
        length = draw.textlength(value, font=F[key])
        text(x - length / 2, y, value, key, fill)
    def tick(x, y, color):
        draw.line([(x - 4, y), (x, y + 4), (x + 7, y - 5)], fill=color, width=2)

    for x in range(0, W, 40):
        for y in range(160, 470, 40):
            draw.point((x, y), fill='#203047')
    text(48, 27, 'MCP GUARD', 'label', GREEN)
    text(48, 55, 'Every tool call. Checked first.', 'title')
    text(48, 108, 'A working sample of the Python and JavaScript gateways', 'subtitle', MUTED)
    box((935, 40, 1152, 84), '#102b27', '#255a4a', 22)
    centered(1043, 53, 'PYTHON  +  JAVASCRIPT', 'label', GREEN)

    steps = ['Allowed call', 'Tool denied', 'Invalid input', 'Rate limited']
    color = [GREEN, RED, AMBER, AMBER][index]
    for i, name in enumerate(steps):
        x = 48 + i * 280
        box((x, 153, x + 263, 195), '#1b2d43' if i == index else '#0d1726', color if i == index else BORDER, 10)
        text(x + 15, 166, f'0{i+1}   {name}', 'label', color if i == index else MUTED)

    # Client, policy gateway, and protected upstream.
    box((48, 258, 285, 415))
    text(70, 278, '01  /  ORIGIN', 'label', MUTED)
    text(70, 310, 'MCP client', 'node')
    text(70, 352, 'tools/call', 'mono', BLUE)
    text(70, 384, 'Authenticated request', 'small', MUTED)
    box((425, 231, 773, 445), '#132439', color if phase > .28 else BORDER, width=2)
    text(448, 250, '02  /  POLICY GATEWAY', 'label', GREEN)
    failed_at = [None, 1, 2, 3][index]
    checks = ['Authentication', 'Tool permissions', 'Input schema', 'Tool rate limit']
    checked = min(4, max(0, int((phase - .23) / .085)))
    for i, label in enumerate(checks):
        y = 295 + i * 34
        passed = checked > i and (failed_at is None or i < failed_at)
        failed = failed_at == i and checked > i
        c = GREEN if passed else color if failed else MUTED
        draw.ellipse((450, y + 1, 466, y + 17), outline=c, width=1)
        if passed:
            tick(457, y + 9, GREEN)
        elif failed:
            draw.line((454, y + 5, 462, y + 13), fill=c, width=2)
            draw.line((462, y + 5, 454, y + 13), fill=c, width=2)
        text(480, y - 1, label, 'small', c)
        if failed:
            text(689, y - 1, 'STOP', 'label', c)
    box((913, 258, 1152, 415), '#122720' if index == 0 and phase > .72 else PANEL)
    text(935, 278, '03  /  PROTECTED', 'label', MUTED)
    text(935, 310, 'MCP server', 'node')
    text(935, 352, 'echo()', 'mono', GREEN if index == 0 and phase > .72 else MUTED)
    text(935, 384, 'Executed once' if index == 0 and phase > .72 else 'Awaiting approved call', 'small', MUTED)

    draw.line((285, 338, 425, 338), fill=BORDER, width=3)
    draw.line((773, 338, 913, 338), fill=BORDER, width=3)
    for x in [417, 905]:
        draw.line([(x - 7, 332), (x, 338), (x - 7, 344)], fill=BORDER, width=2)
    if .07 < phase < .29:
        p = (phase - .07) / .22
        x = 298 + 112 * p
        draw.line((290, 338, x, 338), fill=BLUE, width=3)
        draw.ellipse((x - 7, 331, x + 7, 345), fill=BLUE)
    decided = phase >= .61
    if index == 0 and .61 <= phase < .79:
        x = 786 + 114 * (phase - .61) / .18
        draw.line((773, 338, x, 338), fill=GREEN, width=3)
        draw.ellipse((x - 7, 331, x + 7, 345), fill=GREEN)
    elif index > 0 and decided:
        draw.line((818, 322, 846, 354), fill=color, width=3)
        draw.line((846, 322, 818, 354), fill=color, width=3)
        centered(841, 367, 'Not forwarded', 'small', color)

    # A recorded request and the verified decision; values are drawn from trace.
    text(48, 480, scenario['title'], 'node')
    box((48, 524, 731, 665), '#0d1929')
    text(68, 542, 'REQUEST', 'label', MUTED)
    text(68, 573, f'tool: "{scenario["name"]}"', 'mono', BLUE)
    text(68, 603, 'arguments: ' + json.dumps(scenario['arguments']), 'mono')
    text(68, 638, 'Demo rule: echo allows 1 call per client / 60 seconds', 'small', MUTED)
    box((752, 524, 1152, 665), '#162c29' if index == 0 and decided else '#192437', color if decided else BORDER)
    if decided:
        text(775, 542, f'HTTP {scenario["python"]["status"]}', 'status', color)
        labels = ['ALLOWED', 'TOOL DENIED', 'INVALID INPUT', 'RATE LIMITED']
        text(775, 576, labels[index], 'decision', color)
        descriptions = ['Response: "hello"', 'admin_reset is not permitted', 'text must be a string', 'Second echo call blocked']
        text(775, 625, descriptions[index], 'small', WHITE)
    else:
        text(775, 552, 'CHECKING REQUEST', 'label', BLUE)
        text(775, 590, 'Policy evaluation' + '.' * (1 + int(phase * 10) % 3), 'subtitle', MUTED)

    draw.line((48, 700, 1152, 700), fill=BORDER, width=1)
    text(48, 717, 'Recorded gateway responses  /  Matching results in both implementations', 'small', MUTED)
    centered(1070, 717, f'0{index+1} / 04', 'label', WHITE)
    progress = (index + phase) / 4
    draw.rectangle((0, 755, W * progress, 759), fill=color)
    return image


def main():
    scenarios = collect()
    stills = [render(i, .92, scenario) for i, scenario in enumerate(scenarios)]
    contact = Image.new('RGB', (W, H))
    for i, still in enumerate(stills):
        contact.paste(still.resize((W // 2, H // 2), Image.Resampling.LANCZOS),
                      ((i % 2) * W // 2, (i // 2) * H // 2))
    palette = contact.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    frames = []
    for i, scenario in enumerate(scenarios):
        for frame in range(FRAMES_PER_SCENE):
            image = render(i, frame / (FRAMES_PER_SCENE - 1), scenario)
            frames.append(image.quantize(palette=palette, dither=Image.Dither.NONE))
    path = OUT / 'mcp-guard-demo.gif'
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=FRAME_MS,
                   loop=0, optimize=True, disposal=1)
    stills[0].save(OUT / 'mcp-guard-demo-poster.png')
    contact.save(OUT / 'verification-contact-sheet.png')
    with Image.open(path) as gif:
        assert gif.is_animated and gif.info['loop'] == 0
        duration = 0
        for i in range(gif.n_frames):
            gif.seek(i)
            duration += gif.info['duration']
        print(json.dumps({'path': str(path), 'frames': gif.n_frames, 'duration_ms': duration,
                          'size_bytes': path.stat().st_size, 'verified_statuses': [s['status'] for s in scenarios]}))


if __name__ == '__main__':
    main()
