import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from fleetdeck import vulnscan
from fleetdeck.models import Stack, StackService, StacksResult
from fleetdeck.store import Store
from fleetdeck.vulnscan import ImageScan, ScanError, Scanner, ToolMissing, VulnScans, parse_report, ref_of

DIGEST = "a" * 64
REF = f"ghcr.io/acme/web@sha256:{DIGEST}"


def _vuln(id, pkg, sev, fixed=""):
    return {"VulnerabilityID": id, "PkgName": pkg, "InstalledVersion": "1", "FixedVersion": fixed, "Severity": sev}


REPORT = json.dumps(
    {
        "Results": [
            {
                "Target": "web (debian 12)",
                "Vulnerabilities": [
                    _vuln("CVE-1", "openssl", "CRITICAL", "3.1"),
                    _vuln("CVE-2", "zlib", "HIGH"),
                    _vuln("CVE-3", "libx", "LOW"),
                    _vuln("CVE-1", "openssl", "CRITICAL", "3.1"),
                ],
            },
            {"Target": "app", "Vulnerabilities": None},
        ]
    }
)


def _stacks(*digests):
    services = [
        StackService(name=f"s{i}", image=f"img{i}", status="running", repo_digests=d) for i, d in enumerate(digests)
    ]
    return StacksResult(stacks=[Stack(name="web", services=services)], checked_at=datetime.now(UTC))


class FakeScanner(Scanner):
    def __init__(self, report=REPORT, fail=None):
        super().__init__(None)
        self.report, self.fail, self.updates, self.scans = report, fail, 0, []

    async def update_db(self):
        self.updates += 1

    def db_updated_at(self):
        return datetime.now(UTC) - timedelta(days=1)

    async def scan(self, ref):
        self.scans.append(ref)
        if self.fail:
            raise self.fail
        counts, top, crit = parse_report(self.report, self.min_severity)
        return ImageScan(
            ref=ref,
            counts=counts,
            top=top,
            criticals=crit,
            scanned_at=datetime.now(UTC),
            db_updated_at=self.db_updated_at(),
        )


class Notes:
    def __init__(self):
        self.targets, self.sent = [object()], []

    async def send(self, title, body):
        self.sent.append((title, body))
        return {}


def test_ref_of_needs_a_registry_digest():
    assert ref_of([]) is None
    assert ref_of(["web@sha256:short"]) is None
    assert ref_of(["junk", REF]) == REF


def test_parse_report_counts_dedups_and_filters_top():
    counts, top, crit = parse_report(REPORT, "HIGH")
    assert counts == {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 0, "HIGH": 1, "CRITICAL": 1}
    assert [f.id for f in top] == ["CVE-1", "CVE-2"]
    assert top[0].fixed == "3.1" and crit == ["CVE-1|openssl"]
    assert [f.id for f in parse_report(REPORT, "LOW")[1]] == ["CVE-1", "CVE-2", "CVE-3"]
    assert parse_report("{}", "HIGH")[0]["CRITICAL"] == 0
    with pytest.raises(ScanError):
        parse_report("nope", "HIGH")


class FakeProc:
    def __init__(self, out=b"", err=b"", rc=0, hang=False):
        self.out, self.err, self.returncode, self.hang, self.killed = out, err, rc, hang, False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(10)
        return self.out, self.err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


async def test_scanner_builds_the_command_and_parses(monkeypatch, tmp_path):
    calls = []

    async def fake_exec(*args, **kw):
        calls.append(args)
        return FakeProc(REPORT.encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "metadata.json").write_text('{"UpdatedAt": "2026-10-30T10:00:00Z"}')
    result = await Scanner(tmp_path, ignore_unfixed=True).scan(REF)
    args = list(calls[0])
    assert args[:3] == ["trivy", "image", "--image-src"] and args[-1] == REF
    for flag in ("remote", "vuln", "json", "--quiet", "--skip-db-update", "--ignore-unfixed", str(tmp_path)):
        assert flag in args
    assert result.counts["CRITICAL"] == 1 and result.db_updated_at.year == 2026
    calls.clear()
    await Scanner(tmp_path, ignore_unfixed=False).scan(REF)
    assert "--ignore-unfixed" not in calls[0]


async def test_scanner_errors(monkeypatch, tmp_path):
    async def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    with pytest.raises(ToolMissing):
        await Scanner(tmp_path).update_db()

    async def failing(*a, **k):
        return FakeProc(err=b"boom", rc=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing)
    with pytest.raises(ScanError, match="boom"):
        await Scanner(tmp_path).scan(REF)

    proc = FakeProc(hang=True)

    async def hangs(*a, **k):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hangs)
    monkeypatch.setattr(vulnscan, "SCAN_TIMEOUT", 0.05)
    with pytest.raises(ScanError, match="did not finish"):
        await Scanner(tmp_path).scan(REF)
    assert proc.killed


async def test_run_updates_db_once_scans_each_digest_once_and_caches(tmp_path):
    store, sc = Store(tmp_path / "s.db"), FakeScanner()
    stacks = {"m1": _stacks([REF], [REF]), "m2": _stacks([REF], [])}
    v = VulnScans(sc, store, stacks.get, None)
    await v.scan_run(["m1", "m2"])
    assert sc.updates == 1 and sc.scans == [REF]
    await v.scan_run(["m1"])  # fresh: nothing to do, not even a database update
    assert sc.updates == 1 and sc.scans == [REF]
    out = v.target_vulns("m1")["web"]
    assert out.status == "scanned" and out.worst == "CRITICAL" and out.counts["HIGH"] == 1 and not out.stale
    assert v.target_vulns("m2")["web"].status == "scanned"  # one of its images has no digest


async def test_unscanned_error_and_stale_states(tmp_path):
    v = VulnScans(FakeScanner(fail=ScanError("registry said no")), Store(tmp_path / "s.db"), {"m": _stacks([REF])}.get)
    assert v.target_vulns("m")["web"].status == "not_scanned"
    await v.scan_run(["m"])
    bad = v.target_vulns("m")["web"]
    assert bad.status == "error" and "registry said no" in bad.message
    local = VulnScans(FakeScanner(), None, {"m": _stacks([])}.get).target_vulns("m")["web"]
    assert local.status == "not_scanned" and "no digest" in local.message

    class Old(FakeScanner):
        def db_updated_at(self):
            return datetime.now(UTC) - timedelta(days=8)

    old = VulnScans(Old(), None, {"m": _stacks([REF])}.get)
    await old.scan_run(["m"])
    assert old.target_vulns("m")["web"].stale


async def test_missing_trivy_is_reported_not_raised():
    class Missing(FakeScanner):
        async def update_db(self):
            raise ToolMissing("Trivy is not installed here")

    v = VulnScans(Missing(), None, {"m": _stacks([REF])}.get)
    await v.scan_run(["m"])
    out = v.target_vulns("m")["web"]
    assert out.status == "unavailable" and "not installed" in out.message


async def test_a_crashing_scanner_never_raises():
    class Crash(FakeScanner):
        async def update_db(self):
            raise RuntimeError("bug")

    v = VulnScans(Crash(), None, {"m": _stacks([REF])}.get)
    await v.scan_run(["m"])
    assert not v.running


async def test_notifies_only_about_new_criticals(tmp_path):
    store, notes, sc = Store(tmp_path / "s.db"), Notes(), FakeScanner()
    v = VulnScans(sc, store, {"m": _stacks([REF])}.get, notes)
    await v.scan_run(["m"])
    assert notes.sent == []  # the first scan is a silent baseline: its Criticals are recorded, not announced
    # A new digest of the same repository with the same Critical is not new either.
    v.stacks_of = {"m": _stacks([f"ghcr.io/acme/web@sha256:{'b' * 64}"])}.get
    await v.scan_run(["m"])
    assert notes.sent == []
    sc.report = REPORT.replace("CVE-1", "CVE-9")
    v.stacks_of = {"m": _stacks([f"ghcr.io/acme/web@sha256:{'c' * 64}"])}.get
    await v.scan_run(["m"])
    assert len(notes.sent) == 1 and "1 new Critical vulnerability" in notes.sent[0][0] and "CVE-9" in notes.sent[0][1]
    # Told once: scanning it again (a restart, another machine) stays quiet.
    v2 = VulnScans(sc, store, {"m": _stacks([f"ghcr.io/acme/web@sha256:{'d' * 64}"])}.get, notes)
    await v2.scan_run(["m"])
    assert len(notes.sent) == 1


async def test_baseline_waits_for_a_run_that_scans_something(tmp_path):
    store, notes = Store(tmp_path / "s.db"), Notes()
    v = VulnScans(FakeScanner(fail=ScanError("down")), store, {"m": _stacks([REF])}.get, notes)
    await v.scan_run(["m"])  # nothing scanned: not the baseline yet
    assert store.get_value(vulnscan.BASELINE_KEY) is None
    v.scanner.fail = None
    await v.scan_run(["m"])
    assert store.get_value(vulnscan.BASELINE_KEY) == "1" and notes.sent == []


def test_store_keeps_scans_by_digest(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.load_vuln("d") is None
    s.save_vuln("d", "1")
    s.save_vuln("d", "2")
    assert s.load_vuln("d") == "2"
