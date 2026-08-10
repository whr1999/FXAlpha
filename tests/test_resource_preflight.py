from __future__ import annotations

from domain.data_foundation import ops_common as ctrl


def _write_proc_entry(root, pid: int, cmd_parts: list[str], rss_kb: int) -> None:
    entry = root / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\x00".join(part.encode("utf-8") for part in cmd_parts) + b"\x00")
    (entry / "status").write_text(f"Name:\tpython3\nVmRSS:\t{rss_kb} kB\n", encoding="utf-8")


def test_top_memory_processes_marks_quantgpt_stdio_helper(tmp_path):
    _write_proc_entry(tmp_path, 100, ["python3", "-m", "quantgpt", "--transport", "stdio"], 14 * 1024 * 1024)
    _write_proc_entry(tmp_path, 200, ["python3", "small.py"], 1024)

    rows = ctrl._top_memory_processes(proc_root=tmp_path, limit=2)

    assert rows[0]["pid"] == 100
    assert rows[0]["suspected_stale_helper"] is True
    assert rows[0]["hint"] == "quantgpt_stdio_may_be_stale"
    assert rows[1]["pid"] == 200
    assert rows[1]["suspected_stale_helper"] is False


def test_disk_and_memory_reports_top_processes_when_memory_is_low(monkeypatch, tmp_path):
    _write_proc_entry(tmp_path, 100, ["python3", "-m", "quantgpt", "--transport", "stdio"], 12 * 1024 * 1024)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       32768000 kB\n"
        "MemAvailable:    1048576 kB\n",
        encoding="utf-8",
    )

    class DiskUsage:
        free = 100 * 1024**3
        total = 200 * 1024**3

    monkeypatch.setattr(ctrl.shutil, "disk_usage", lambda path: DiskUsage())

    report = ctrl._disk_and_memory(meminfo_path=meminfo, proc_root=tmp_path)

    assert report["mem_ok"] is False
    assert report["top_memory_processes"][0]["pid"] == 100
    assert report["suspected_stale_helpers"][0]["hint"] == "quantgpt_stdio_may_be_stale"
