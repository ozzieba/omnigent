"""Browser lifecycle checks using the real surface and a synthetic same-origin Lab.

No host, credentials, kernels, or model calls. Requires installed web dependencies
and Node 22+. The iframe fixture deliberately stalls/restores/rejects Lab startup.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

_ROOT = Path(__file__).resolve().parents[3]
_HTML = """<!doctype html><html><head>
<link rel="stylesheet" href="/src/components/docloop/docloop-chat-layout.css">
</head><body><div id="root"></div>
<script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type;
window.__vite_plugin_react_preamble_installed__ = true;
const {default: React} = await import('/node_modules/.vite/deps/react.js');
const {default: {createRoot}} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {NativeNotebookSurface} = await import('/src/components/docloop/NativeNotebookSurface.tsx');
const root = createRoot(document.getElementById('root'));
window.showSurface = (active = true) => root.render(
  React.createElement(NativeNotebookSurface, {sessionId:'fixture', active}));
window.showSurface();
</script></body></html>"""
_LAB = """<!doctype html><html><body>Local notebook fixture<script>
window.jupyterapp = {
  restored: READY ? Promise.resolve() : new Promise((resolve, reject) => {
    window.finishRestore = resolve; window.failRestore = reject;
  }),
  shell: {mode: 'multiple-document', collapseLeft() {}, collapseRight() {},
    currentWidget: {sessionContext: {kernelDisplayStatus: 'idle'}}}
};
</script></body></html>"""


@pytest.fixture
def surface_url(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "OMNIGENT_URL": "http://127.0.0.1:1"}
    with (tmp_path / "vite.log").open("w") as log:
        process = subprocess.Popen(
            [
                "node",
                "node_modules/vite/bin/vite.js",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--strictPort",
            ],
            cwd=_ROOT / "web",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(200):
                if process.poll() is not None:
                    pytest.fail((tmp_path / "vite.log").read_text())
                try:
                    if httpx.get(url + "/@vite/client").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Local Vite fixture did not start")
            yield url
        finally:
            process.terminate()
            process.wait(timeout=15)


@pytest.mark.parametrize("failure", ["timeout", "rejection"])
async def test_native_load_recovery(surface_url, failure, tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        loads = []
        descriptors = []

        async def api(route):
            path = route.request.url.split("/docloop", 1)[1]
            if path == "/document":
                await route.fulfill(json={"format": "ipynb"})
            elif path == "/jupyter":
                descriptors.append(path)
                await route.fulfill(
                    json={"url": "/v1/sessions/fixture/docloop/jupyter/lab/tree/task.ipynb"}
                )
            elif path.startswith("/jupyter/lab/tree/"):
                loads.append(path)
                await route.fulfill(
                    content_type="text/html",
                    body=_LAB.replace("READY", "true" if len(loads) > 1 else "false"),
                )
            elif path.startswith("/versions"):
                await route.fulfill(
                    json={
                        "schema_version": 1,
                        "session_id": "fixture",
                        "binding_id": "b" * 64,
                        "versions": [],
                    }
                )
            else:
                await route.fulfill(status=404)

        await page.route("**/v1/**", api)

        async def surface_fixture(route):
            await route.fulfill(content_type="text/html", body=_HTML)

        await page.route("**/surface-fixture", surface_fixture)
        await page.clock.install()
        await page.goto(surface_url + "/surface-fixture")
        frame = page.frame_locator("iframe")
        await expect(frame.locator("body")).to_contain_text("Local notebook fixture")
        # Let the parent attach its restoration promise observer first.
        await page.clock.run_for(500)
        if failure == "timeout":
            await page.clock.fast_forward(120_000)
            await expect(page.get_by_role("alert")).to_contain_text("did not finish loading")
        else:
            await frame.locator("body").evaluate(
                '() => window.failRestore(new Error("fixture failure"))'
            )
            await expect(page.get_by_role("alert")).to_contain_text("could not restore")
        evidence = Path(os.environ.get("OMNIGENT_E2E_EVIDENCE_DIR", tmp_path))
        evidence.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(evidence / f"native-load-{failure}.png"), full_page=True)
        await page.clock.fast_forward(120_000)
        assert len(loads) == len(descriptors) == 1
        await page.get_by_role("button", name="Try again").click()
        await expect(frame.locator("body")).to_contain_text("Local notebook fixture")
        await page.clock.run_for(750)
        await expect(page.get_by_role("status")).to_have_text("Kernel: idle")
        await page.evaluate('window.savedFrame = document.querySelector("iframe")')
        await page.evaluate("window.showSurface(false)")
        await page.evaluate("window.showSurface(true)")
        await page.get_by_role("button", name="History", exact=True).click()
        await page.get_by_role("button", name="JupyterLab", exact=True).click()
        await page.clock.fast_forward(240_000)
        assert await page.evaluate('window.savedFrame === document.querySelector("iframe")')
        assert len(loads) == len(descriptors) == 2
        await expect(page.get_by_role("alert")).to_have_count(0)
        assert errors == []
        await browser.close()
