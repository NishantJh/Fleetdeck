"""Opt-in vulnerability scanning of the Docker images found on machines, with Trivy.

Trivy runs here, inside the dashboard's container, never on the machines: it reads each image's layers
from its registry by digest (``trivy image --image-src remote``). Nothing runs on, or is written to, a
machine. Images without a registry digest (built locally) can't be fetched that way and show as "not scanned".

Results are kept in the store by digest, so an image shared by several machines is scanned once. The
vulnerability database is refreshed once per scan run; every scan in the run then skips its own update.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import Stack, StacksResult
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)

SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
RANK = {s: i for i, s in enumerate(SEVERITIES)}
TOP_LIMIT = 25
CRITICAL_LIMIT = 200
SCAN_TIMEOUT = 600.0
DB_TIMEOUT = 300.0
# A scan is reused for this long; after that the next run scans the image again, because the database
# learns about new vulnerabilities every day.
CACHE_TTL = timedelta(hours=24)
# Results made with a database older than this are shown as stale.
STALE_AFTER = timedelta(days=7)
SEEN_KEY = "vuln_critical_seen"
BASELINE_KEY = "vuln_baseline_done"


class ScanError(Exception):
    pass


class ToolMissing(ScanError):
    """Trivy is not installed here (a source checkout; the Docker image has it)."""


class Finding(BaseModel):
    id: str
    pkg: str
    installed: str = ""
    fixed: str = ""
    severity: str


class ImageScan(BaseModel):
    ref: str  # repo@sha256:...
    counts: dict[str, int] = Field(default_factory=dict)
    top: list[Finding] = Field(default_factory=list)  # at or above the minimum severity, worst first
    criticals: list[str] = Field(default_factory=list)  # "<id>|<pkg>" of every Critical, for notifications
    scanned_at: datetime
    db_updated_at: datetime | None = None


class StackVulns(BaseModel):
    """What the dashboard shows for one stack."""

    status: Literal["scanned", "not_scanned", "error", "unavailable"] = "not_scanned"
    # Severity counts over the stack's scanned images (all severities).
    counts: dict[str, int] = Field(default_factory=dict)
    top: list[Finding] = Field(default_factory=list)
    worst: str | None = None  # the worst severity at or above the minimum, for the badge
    scanned_at: datetime | None = None
    stale: bool = False
    images: int = 0  # images in the stack
    scanned: int = 0  # of which scanned
    message: str = ""


def ref_of(repo_digests: list[str]) -> str | None:
    """The reference to scan an image by: its first ``repo@sha256:digest``. None when it has none."""
    for d in repo_digests:
        repo, sep, digest = d.partition("@sha256:")
        if sep and repo and len(digest) == 64:
            return d
    return None


def parse_report(text: str, min_severity: str) -> tuple[dict[str, int], list[Finding], list[str]]:
    """Counts per severity, the worst findings at or above ``min_severity`` and the Criticals, from Trivy's JSON."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ScanError(f"Trivy printed something that is not JSON: {e}") from None
    seen: dict[tuple[str, str, str], Finding] = {}
    for result in (data.get("Results") or []) if isinstance(data, dict) else []:
        for v in result.get("Vulnerabilities") or []:
            sev = str(v.get("Severity", "UNKNOWN")).upper()
            f = Finding(
                id=str(v.get("VulnerabilityID", "")),
                pkg=str(v.get("PkgName", "")),
                installed=str(v.get("InstalledVersion", "")),
                fixed=str(v.get("FixedVersion", "")),
                severity=sev if sev in RANK else "UNKNOWN",
            )
            seen.setdefault((f.id, f.pkg, f.installed), f)
    findings = sorted(seen.values(), key=lambda f: (-RANK[f.severity], f.id, f.pkg))
    counts = {s: 0 for s in SEVERITIES}
    for f in findings:
        counts[f.severity] += 1
    floor = RANK.get(min_severity.upper(), RANK["HIGH"])
    top = [f for f in findings if RANK[f.severity] >= floor][:TOP_LIMIT]
    criticals = [f"{f.id}|{f.pkg}" for f in findings if f.severity == "CRITICAL"][:CRITICAL_LIMIT]
    return counts, top, criticals


class Scanner:
    """Runs the Trivy binary."""

    def __init__(self, cache_dir: Path, ignore_unfixed: bool = True, min_severity: str = "HIGH", binary: str = "trivy"):
        self.cache_dir = cache_dir
        self.ignore_unfixed = ignore_unfixed
        self.min_severity = min_severity
        self.binary = binary

    async def _run(self, args: list[str], timeout: float) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            raise ToolMissing("Trivy is not installed here (it is in the Docker image)") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise ScanError(f"Trivy did not finish in {timeout / 60:.0f} minutes") from None
        if proc.returncode != 0:
            raise ScanError(f"Trivy exited with code {proc.returncode}: {err.decode(errors='replace').strip()[-300:]}")
        return out.decode(errors="replace")

    async def update_db(self) -> None:
        await self._run(["image", "--download-db-only", "--cache-dir", str(self.cache_dir), "--quiet"], DB_TIMEOUT)

    def db_updated_at(self) -> datetime | None:
        try:
            meta = json.loads((self.cache_dir / "db" / "metadata.json").read_text())
            return datetime.fromisoformat(str(meta["UpdatedAt"]).replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def scan(self, ref: str) -> ImageScan:
        args = ["image", "--image-src", "remote", "--scanners", "vuln", "--format", "json"]
        args += ["--cache-dir", str(self.cache_dir), "--quiet", "--skip-db-update"]
        if self.ignore_unfixed:
            args.append("--ignore-unfixed")
        out = await self._run([*args, ref], SCAN_TIMEOUT)
        counts, top, criticals = parse_report(out, self.min_severity)
        return ImageScan(
            ref=ref,
            counts=counts,
            top=top,
            criticals=criticals,
            scanned_at=datetime.now(UTC),
            db_updated_at=self.db_updated_at(),
        )


class VulnScans:
    def __init__(
        self,
        scanner: Scanner,
        store: Store | None,
        stacks_of: Callable[[str], StacksResult | None],
        notifier: Notifier | None = None,
    ):
        self.scanner = scanner
        self.store = store
        self.stacks_of = stacks_of
        self.notifier = notifier
        self._pending = 0
        self._lock = asyncio.Lock()
        self.unavailable = ""  # why Trivy can't run here, while it can't
        self._mem: dict[str, ImageScan] = {}  # when there is no store
        self._errors: dict[str, str] = {}

    def _load(self, ref: str) -> ImageScan | None:
        digest = ref.partition("@")[2]
        if self.store is None:
            return self._mem.get(digest)
        raw = self.store.load_vuln(digest)
        if raw is None:
            return None
        try:
            return ImageScan.model_validate_json(raw)
        except ValueError:
            return None

    def _save(self, result: ImageScan) -> None:
        digest = result.ref.partition("@")[2]
        if self.store is None:
            self._mem[digest] = result
        else:
            self.store.save_vuln(digest, result.model_dump_json())

    def _fresh(self, ref: str) -> bool:
        cached = self._load(ref)
        return cached is not None and datetime.now(UTC) - cached.scanned_at < CACHE_TTL

    @property
    def running(self) -> bool:
        return self._pending > 0

    async def scan_run(self, target_ids: list[str]) -> None:
        """Scan the images of these machines' stacks that have no fresh result. The database is updated once.
        Runs one at a time. Never raises: a failing scan is logged and shown as unavailable."""
        self._pending += 1
        try:
            async with self._lock:
                await self._run(target_ids)
        except Exception:  # noqa: BLE001 - scanning must never break what called it
            log.exception("scanning Docker images failed")
        finally:
            self._pending -= 1

    async def _run(self, target_ids: list[str]) -> None:
        todo: dict[str, str] = {}
        for tid in target_ids:
            result = self.stacks_of(tid)
            if result is None or result.docker != "yes":
                continue
            for stack in result.stacks:
                for svc in stack.services:
                    ref = ref_of(svc.repo_digests)
                    if ref and not self._fresh(ref):
                        todo[ref.partition("@")[2]] = ref
        if not todo:
            return
        try:
            await self.scanner.update_db()
        except ToolMissing as e:
            log.warning("image scans are unavailable: %s", e)
            self.unavailable = str(e)
            return
        except ScanError as e:
            log.warning("could not update the vulnerability database: %s", e)  # scans use the old one, if any
        self.unavailable = ""
        fresh: list[ImageScan] = []
        for digest, ref in todo.items():
            try:
                scan = await self.scanner.scan(ref)
            except ScanError as e:
                log.warning("scanning %s failed: %s", ref, e)
                self._errors[digest] = str(e)
                continue
            self._errors.pop(digest, None)
            self._save(scan)
            fresh.append(scan)
        await self._notify(fresh)

    async def _notify(self, scans: list[ImageScan]) -> None:
        """One message for the Criticals not told about before (same vulnerability in the same package of the
        same image repository counts once, whatever the digest). The first run that scans anything only records
        what it finds: scanning is on by default, so an existing install must not get a flood of old news."""
        if self.store is None or not scans:
            return
        try:
            seen = set(json.loads(self.store.get_value(SEEN_KEY) or "[]"))
        except ValueError:
            seen = set()
        new: list[str] = []
        for scan in scans:
            repo = scan.ref.partition("@")[0]
            for c in scan.criticals:
                key = f"{repo}|{c}"
                if key not in seen:
                    new.append(key)
        if self.store.get_value(BASELINE_KEY) is None:
            self.store.set_value(SEEN_KEY, json.dumps(sorted(seen | set(new))))
            self.store.set_value(BASELINE_KEY, "1")
            return
        if not new or self.notifier is None:
            return
        lines = [f"{k.split('|')[0]}: {k.split('|')[1]} in {k.split('|')[2]}" for k in new[:10]]
        if len(new) > 10:
            lines.append(f"… and {len(new) - 10} more")
        n = len(new)
        errors = await self.notifier.send(
            f"Homelab: {n} new Critical vulnerabilit{'ies' if n > 1 else 'y'}", "\n".join(lines)
        )
        if len(errors) < len(self.notifier.targets):
            self.store.set_value(SEEN_KEY, json.dumps(sorted(seen | set(new))))

    def stack_vulns(self, stack: Stack) -> StackVulns:
        refs = {r.partition("@")[2]: r for s in stack.services if (r := ref_of(s.repo_digests))}
        images = len({s.image for s in stack.services})
        out = StackVulns(images=images)
        if not refs:
            out.message = "Built on this machine or not from a registry: there is no digest to scan"
            return out
        counts = {s: 0 for s in SEVERITIES}
        top: list[Finding] = []
        scanned_at = db_at = None
        errors = []
        for digest, ref in refs.items():
            scan = self._load(ref)
            if scan is None:
                if digest in self._errors:
                    errors.append(self._errors[digest])
                continue
            out.scanned += 1
            for s, n in scan.counts.items():
                counts[s] = counts.get(s, 0) + n
            top += scan.top
            scanned_at = min(scanned_at or scan.scanned_at, scan.scanned_at)
            if scan.db_updated_at:
                db_at = min(db_at or scan.db_updated_at, scan.db_updated_at)
        if not out.scanned:
            if self.unavailable:
                out.status, out.message = "unavailable", f"Scan unavailable: {self.unavailable}"
            elif errors:
                out.status, out.message = "error", f"Scan unavailable: {errors[0]}"
            else:
                out.message = "Not scanned yet"
            return out
        floor = RANK.get(self.scanner.min_severity.upper(), RANK["HIGH"])
        top.sort(key=lambda f: (-RANK[f.severity], f.id, f.pkg))
        out.status, out.counts, out.top = "scanned", counts, top[:TOP_LIMIT]
        out.worst = next((s for s in reversed(SEVERITIES) if RANK[s] >= floor and counts.get(s)), None)
        out.scanned_at = scanned_at
        out.stale = db_at is not None and datetime.now(UTC) - db_at > STALE_AFTER
        if errors:
            out.message = f"{len(errors)} image(s) could not be scanned: {errors[0]}"
        return out

    def target_vulns(self, target_id: str) -> dict[str, StackVulns]:
        result = self.stacks_of(target_id)
        if result is None:
            return {}
        return {s.name: self.stack_vulns(s) for s in result.stacks}
