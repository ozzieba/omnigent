"""Exercise ordinary CLI startup with private stores and no server listener."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.db.utils import _engine_cache, _engine_lock
from omnigent.server import app as server_app
from omnigent.server import auth
from omnigent.server.host_admission_bootstrap import create_cli_host_admission
from omnigent.stores.host_admission_store import Store

HOST = "5" * 32
OWNER = "owner@example.test"


class FixtureAuth(auth.AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("x-test-subject")


class CliBootstrapTests(unittest.TestCase):
    def check_startup(self, config):
        import uvicorn.server

        captured = {}
        original = server_app.create_app

        def create_app(**kwargs):
            captured.update(kwargs)
            app = original(**kwargs)
            captured["app"] = app
            return app

        with tempfile.TemporaryDirectory(prefix="admission-cli-") as directory:
            root = Path(directory)
            settings = {"host_maintenance_ready": True, "host_admission_ready": True}
            if config is not None:
                settings["host_admission"] = config
            config_path = root / "server.yaml"
            config_path.write_text(yaml.safe_dump(settings))
            uri = f"sqlite:///{root / 'private.db'}"
            with (
                patch.object(server_app, "create_app", create_app),
                patch.object(uvicorn.server.Server, "run", lambda self: None),
                patch.object(auth, "create_auth_provider", FixtureAuth),
                patch("omnigent.cli._assert_server_port_bindable", lambda host, port: None),
                patch(
                    "omnigent.process_logging.configure_process_logging",
                    return_value=root / "server.log",
                ),
                patch.dict(
                    os.environ,
                    {
                        "OMNIGENT_AUTH_PROVIDER": "header",
                        "OMNIGENT_CONFIG_HOME": str(root / "config-home"),
                        "OMNIGENT_DATA_DIR": str(root / "data"),
                    },
                ),
            ):
                result = CliRunner().invoke(
                    cli,
                    [
                        "server",
                        "-c",
                        str(config_path),
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "44999",
                        "--no-open",
                        "--database-uri",
                        uri,
                        "--artifact-location",
                        str(root / "artifacts"),
                    ],
                )
            self.assertEqual(result.exit_code, 0, (result.output, result.exception))
            adapter = captured["host_admission"]
            host_store = captured["host_store"]
            engine = host_store._engine
            store = Store(engine)
            host_store.upsert_on_connect(HOST, "synthetic", OWNER)

            async def verify():
                try:
                    self.assertIsNone(store.binding_for_host(0, HOST))
                    if config == {"enabled": True}:
                        self.assertIs(adapter.store.engine, engine)
                        self.assertIs(captured["app"].state.host_admission, adapter)
                        self.assertNotIn("host_maintenance_ready", captured)
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=captured["app"]),
                            base_url="http://synthetic.test",
                            headers={"x-test-subject": OWNER},
                        ) as client:
                            response = await client.post(f"/v1/hosts/{HOST}/admission")
                        self.assertEqual(response.status_code, 503)
                        self.assertIsNone(store.binding_for_host(0, HOST))
                    else:
                        self.assertIsNone(adapter)
                        self.assertFalse(hasattr(captured["app"].state, "host_admission"))
                finally:
                    if adapter is not None:
                        await adapter.aclose()

            try:
                asyncio.run(verify())
            finally:
                with _engine_lock:
                    self.assertIs(_engine_cache.pop(uri), engine)
                engine.dispose()

    def test_default_cli_does_not_mount_or_enroll_admission(self):
        self.check_startup(None)

    def test_disabled_cli_does_not_mount_or_enroll_admission(self):
        self.check_startup({"enabled": False})

    def test_enabled_cli_binds_canonical_engine_without_granting_readiness(self):
        self.check_startup({"enabled": True})

    def test_invalid_config_rejected_before_worker_allocation(self):
        cases = (
            True,
            "true",
            [],
            {"enabled": 1},
            {"enabled": "true"},
            {"enabled": True, "maintenance_ready": True},
            {"ready": True},
        )
        with patch(
            "omnigent.server.host_admission_bootstrap.AsyncAdmission",
            side_effect=AssertionError("WORKER_ALLOCATION"),
        ):
            for config in cases:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    create_cli_host_admission(config, host_store=None, auth_provider=FixtureAuth())

    def test_authentication_required_before_worker_allocation(self):
        with (
            patch(
                "omnigent.server.host_admission_bootstrap.AsyncAdmission",
                side_effect=AssertionError("WORKER_ALLOCATION"),
            ),
            self.assertRaisesRegex(ValueError, "authentication"),
        ):
            create_cli_host_admission({"enabled": True}, host_store=None, auth_provider=None)
