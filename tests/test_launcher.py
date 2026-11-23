import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import launch
from main import create_app
from ragcore.workspace_id import workspace_id


class LauncherWorkspaceTests(unittest.TestCase):
    def test_health_identifies_bound_directory_without_returning_its_path(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "行业工作区"
            with TestClient(create_app(data, seed=False)) as client:
                health = client.get("/healthz").json()
            self.assertEqual(health["status"], "ok")
            self.assertRegex(health["workspace_id"], r"^[0-9a-f]{64}$")
            self.assertEqual(health["workspace_id"], workspace_id(data / ".." / data.name))
            self.assertNotEqual(health["workspace_id"], workspace_id(data.parent / "other"))
            self.assertNotIn("行业工作区", str(health))
            if os.name == "nt":
                self.assertEqual(workspace_id(data), workspace_id(str(data).swapcase()))

    def test_port_selection_skips_foreign_and_unidentified_instances(self):
        with tempfile.TemporaryDirectory() as root, patch.object(launch, "ROOT", Path(root)), \
                patch.dict(os.environ, {"RAG_DATA_DIR": "workspace-a"}), \
                patch.object(launch, "DEFAULT_PORTS", range(8781, 8785)), \
                patch.object(launch.socket, "socket") as sockets, \
                patch.object(launch, "response_json") as response, \
                patch.object(launch, "supports_embedded_reader", return_value=True):
            expected = workspace_id(Path(root) / "workspace-a")
            ports = {8781: {"status": "ok", "workspace_id": workspace_id(Path(root) / "workspace-b")},
                     8782: {"status": "ok"}}
            client = sockets.return_value.__enter__.return_value

            def bind(address):
                if address[1] in ports:
                    raise OSError("port already in use")

            client.bind.side_effect = bind
            schema = create_app().openapi()
            response.side_effect = lambda port, path: ports[port] if path == "/healthz" else schema

            self.assertEqual(launch.select_port(None), (8783, "offline"))
            self.assertEqual(launch.select_port(8781), (8781, "other"))
            # A matching service on a later port takes priority over starting another copy.
            ports[8784] = {"status": "ok", "workspace_id": expected}
            self.assertEqual(launch.select_port(None), (8784, "ready"))
