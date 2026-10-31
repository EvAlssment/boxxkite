import json
import sys
import tempfile
import unittest
from pathlib import Path

from runner import DATASET, DatasetError, _run_task, load_dataset


class DatasetTests(unittest.TestCase):
    def test_starter_dataset_is_valid_and_has_unique_ids(self):
        tasks = load_dataset()
        self.assertEqual(len(tasks), 3)
        self.assertEqual(len({task["id"] for task in tasks}), len(tasks))

    def test_rejects_parent_traversal_in_setup_files(self):
        document = json.loads(DATASET.read_text(encoding="utf-8"))
        document["tasks"][0]["setup_files"]["../escape"] = "no"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(DatasetError, "safe relative paths"):
                load_dataset(path)

    def test_rejects_duplicate_ids(self):
        document = json.loads(DATASET.read_text(encoding="utf-8"))
        document["tasks"][1]["id"] = document["tasks"][0]["id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(DatasetError, "duplicate task id"):
                load_dataset(path)

    def test_runner_uses_isolated_workspace_and_checks_outputs(self):
        task = {
            "id": "unit-task",
            "setup_files": {"nested/input.txt": "starter"},
            "checks": [{"path": "answer.txt", "text": "done"}],
        }
        command = [sys.executable, "-c", "from pathlib import Path; Path('answer.txt').write_text('done')"]
        self.assertTrue(_run_task(task, command, 10))
        self.assertFalse(Path("answer.txt").exists())


if __name__ == "__main__":
    unittest.main()
