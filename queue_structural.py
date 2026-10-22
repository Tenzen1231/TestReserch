"""Pinned Mergiraf proposals. A syntactic merge never authorizes publication.

Gate must validate the returned files with its immutable behavioral oracle.
No Git checkout, model, shell or candidate Python is executed by this module.
GitHub execution uses Docker; local native execution is available for probes.
"""

import ast
import hashlib
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid


PRODUCT_PATHS = frozenset({"money.py", "receipt.py", "email_receipt.py", "feature_a.py", "feature_b.py", "feature_c.py"})
VERSION = "0.20.0"
IMAGE = "python@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2"
PINNED_BINARIES = {
    "linux-x86_64-musl": "47b64ee837b110acd48a633c0bd13ff09e6a794b9739f896362464e64f7c7217",
    "windows-x86_64": "05fae92dae2ad8dfaa03071ec5b7825d5d4c901bd6d69dd59604724b9e795ca1",
}
PINNED_ARCHIVES = {
    "linux-x86_64-musl": {
        "url": "https://codeberg.org/mergiraf/mergiraf/releases/download/v0.20.0/mergiraf_x86_64-unknown-linux-musl.tar.gz",
        "sha256": "fc77469be936b02ab9021b984a3aebc9250a21ae3d264f2ef2198eb8b31b2a7b"},
    "windows-x86_64": {
        "url": "https://codeberg.org/mergiraf/mergiraf/releases/download/v0.20.0/mergiraf_x86_64-pc-windows-gnu.zip",
        "sha256": "4f5e77e973febda3cacd2b52f0c0b2756854e2bff1377af69159cf0aa4051397"},
}
MAX_INPUT_BYTES = 64 * 1024
MAX_TOTAL_INPUT_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
MAX_TOTAL_OUTPUT_BYTES = 256 * 1024
MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_TIMEOUT = 30
MARKERS = re.compile(r"(?m)^(?:<{7}|>{7}|\|{7})(?: |$)|^={7}$")


class StructuralError(RuntimeError):
    def __init__(self, reason, detail, metadata=None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.metadata = dict(metadata or {})


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def checked_paths(value):
    if (not isinstance(value, (list, tuple, set, frozenset)) or not value or len(value) > len(PRODUCT_PATHS)
            or any(not isinstance(item, str) or item not in PRODUCT_PATHS for item in value)
            or len(set(value)) != len(value)):
        raise StructuralError("structural_invalid_paths", "Only the six explicit product Python paths are supported")
    return frozenset(value)


class StructuralRepairClient:
    def __init__(self, binary, allowed_paths=PRODUCT_PATHS, timeout=MAX_TIMEOUT, use_docker=None):
        if not isinstance(binary, (str, Path)) or not str(binary) or not Path(binary).is_absolute():
            raise StructuralError("structural_unavailable", "MERGIRAF_BINARY must identify an installed absolute binary path")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
            raise StructuralError("structural_invalid_timeout", "Structural timeout must be positive and at most 30 seconds")
        self.binary = Path(binary)
        self.allowed = checked_paths(allowed_paths)
        self.timeout = timeout
        # A caller cannot disable Docker in GitHub Actions.
        github = os.environ.get("GITHUB_ACTIONS") == "true"
        self.use_docker = github or (os.environ.get("QUEUE_STRUCTURAL_DOCKER") == "true" if use_docker is None else use_docker)
        self.docker = shutil.which("docker") if self.use_docker else None
        if self.use_docker and not self.docker:
            raise StructuralError("structural_unavailable", "Docker is required for structural merging in GitHub")
        machine = platform.machine().lower()
        if machine not in ("x86_64", "amd64"):
            raise StructuralError("structural_unsupported_platform", "Only pinned x86_64 binaries are configured")
        self.platform = "linux-x86_64-musl" if self.use_docker or platform.system() == "Linux" else "windows-x86_64"
        if not self.use_docker and platform.system() not in ("Linux", "Windows"):
            raise StructuralError("structural_unsupported_platform", "This native platform has no pinned binary")

    @classmethod
    def from_environment(cls, allowed_paths=PRODUCT_PATHS, timeout=MAX_TIMEOUT):
        return cls(os.environ.get("MERGIRAF_BINARY"), allowed_paths, timeout)

    def inputs(self, context):
        if not isinstance(context, dict):
            raise StructuralError("structural_invalid_context", "Structural context must be an object")
        scope = self.allowed
        if "allowed_paths" in context:
            narrower = checked_paths(context["allowed_paths"])
            if not narrower <= scope:
                raise StructuralError("structural_invalid_paths", "Context cannot expand structural scope")
            scope = narrower
        conflicts = checked_paths(context.get("conflict_paths"))
        if not conflicts <= scope:
            raise StructuralError("structural_invalid_paths", "Conflicting paths are outside the approved scope")
        result, total = {}, 0
        for path in sorted(conflicts):
            result[path] = {}
            for side in ("common_ancestor", "current_target", "submission"):
                snapshot = context.get(side)
                content = snapshot.get(path) if isinstance(snapshot, dict) else None
                if not isinstance(content, str) or "\0" in content or MARKERS.search(content):
                    raise StructuralError("structural_invalid_input", "Three ordinary UTF-8 snapshots without conflict markers are required")
                try:
                    value = content.encode("utf-8")
                except UnicodeError:
                    raise StructuralError("structural_invalid_input", "Input is not valid UTF-8 text") from None
                total += len(value)
                if len(value) > MAX_INPUT_BYTES or total > MAX_TOTAL_INPUT_BYTES:
                    raise StructuralError("structural_input_too_large", "Structural input exceeds its size limit")
                result[path][side] = value
        return result

    def verified_binary(self):
        try:
            if self.binary.is_symlink() or not self.binary.is_file() or self.binary.stat().st_size > MAX_BINARY_BYTES:
                raise StructuralError("structural_invalid_binary", "Mergiraf must be a bounded regular binary file")
            contents = self.binary.read_bytes()
        except OSError:
            raise StructuralError("structural_unavailable", "The pinned Mergiraf binary cannot be read") from None
        if sha256(contents) != PINNED_BINARIES[self.platform]:
            raise StructuralError("structural_checksum_mismatch", "Mergiraf binary does not match the pinned release")
        return contents

    @staticmethod
    def clean_environment(directory):
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
        for name in ("home", "cache", "config", "data", "tmp", "empty-bin"):
            (directory / name).mkdir(exist_ok=True)
        env.update(PATH=str(directory / "empty-bin"), HOME=str(directory / "home"), USERPROFILE=str(directory / "home"),
                   XDG_CACHE_HOME=str(directory / "cache"), XDG_CONFIG_HOME=str(directory / "config"),
                   XDG_DATA_HOME=str(directory / "data"), TEMP=str(directory / "tmp"), TMP=str(directory / "tmp"),
                   APPDATA=str(directory / "config"), LOCALAPPDATA=str(directory / "data"))
        return env

    def invoke(self, binary, arguments, directory, environment, deadline, inputs=None, output=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StructuralError("structural_timeout", "Structural merge exceeded its time budget")
        container = "council-structural-" + uuid.uuid4().hex if self.use_docker else None
        command = [str(binary), *arguments]
        if container:
            command = [self.docker, "run", "--rm", "--pull=never", "--name", container, "--network=none", "--read-only",
                       "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=32",
                       "--memory=256m", "--cpus=1", "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
                       "--env", "HOME=/tmp/home", "--env", "XDG_CACHE_HOME=/tmp/cache", "--env", "XDG_DATA_HOME=/tmp/data",
                       "--env", "XDG_CONFIG_HOME=/tmp/config", "--env", "PATH=/nonexistent",
                       "--mount", "type=bind,src=" + str(binary) + ",dst=/tool/mergiraf,readonly"]
            if inputs:
                command += ["--mount", "type=bind,src=" + str(inputs) + ",dst=/inputs,readonly",
                            "--mount", "type=bind,src=" + str(output) + ",dst=/output", "--workdir=/inputs"]
            command += ["--entrypoint", "/tool/mergiraf", IMAGE, *arguments]
        try:
            process = subprocess.run(command, cwd=directory, env=environment, capture_output=True,
                                     timeout=min(remaining, 10), shell=False)
            if len(process.stdout) > MAX_DIAGNOSTIC_BYTES or len(process.stderr) > MAX_DIAGNOSTIC_BYTES:
                raise StructuralError("structural_output_too_large", "Structural diagnostics exceed their size limit")
            return process
        except subprocess.TimeoutExpired:
            raise StructuralError("structural_timeout", "Structural merge exceeded its time budget") from None
        except OSError:
            raise StructuralError("structural_execution_failed", "The structural merge process could not run") from None
        finally:
            if container:
                # Terminating `docker run` alone does not guarantee the parser
                # stopped. Only this invocation's unpredictable name is touched.
                try:
                    subprocess.run([self.docker, "kill", container], env=environment, capture_output=True, timeout=5)
                    subprocess.run([self.docker, "rm", container], env=environment, capture_output=True, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def propose(self, context):
        started = time.monotonic()
        metadata = {"stage": "structural", "tool": "mergiraf", "version": VERSION,
                    "sha256": PINNED_BINARIES[self.platform], "platform": self.platform, "files": [],
                    "binary_verified": False, "version_verified": False,
                    "sandbox": {"kind": "docker", "image": IMAGE, "network": "none"} if self.use_docker else {"kind": "local_process"}}
        try:
            data = self.inputs(context)
            metadata["conflict_paths"] = list(data)
            contents = self.verified_binary()
            metadata["binary_verified"] = True
            with tempfile.TemporaryDirectory(prefix="council-structural-") as temporary:
                directory = Path(temporary)
                environment = self.clean_environment(directory)
                # Execute a private copy of the verified bytes, not an ambient PATH command.
                binary = directory / ("mergiraf.exe" if self.platform == "windows-x86_64" else "mergiraf")
                binary.write_bytes(contents)
                binary.chmod(0o555)
                deadline = started + self.timeout
                version = self.invoke(binary, ["--version"], directory, environment, deadline)
                metadata["version_exit_code"] = version.returncode
                if version.returncode != 0 or version.stdout.strip() != ("mergiraf " + VERSION).encode():
                    raise StructuralError("structural_version_mismatch", "Mergiraf did not confirm its pinned version")
                metadata["version_verified"] = True
                files, output_size = {}, 0
                for number, (path, sides) in enumerate(data.items()):
                    inputs, output = directory / ("inputs-" + str(number)), directory / ("output-" + str(number))
                    inputs.mkdir(mode=0o755)
                    output.mkdir(mode=0o777)
                    output.chmod(0o777)  # The Docker uid must write its isolated result.
                    paths = []
                    for side, filename in (("common_ancestor", "base.py"), ("current_target", "target.py"), ("submission", "source.py")):
                        location = inputs / filename
                        location.write_bytes(sides[side])
                        location.chmod(0o444)
                        paths.append(location)
                    destination = output / "merged.py"
                    record = {"path": path, "inputs_sha256": {side: sha256(value) for side, value in sides.items()}}
                    metadata["files"].append(record)
                    inside = ["/inputs/" + path.name for path in paths] if self.use_docker else list(map(str, paths))
                    arguments = ["merge", *inside, "--language", "python", "--allow-parse-errors=false", "--compact=false",
                                 "--timeout", "5000", "--output", "/output/merged.py" if self.use_docker else str(destination)]
                    process = self.invoke(binary, arguments, directory, environment, deadline, inputs, output)
                    record.update(exit_code=process.returncode, stdout_sha256=sha256(process.stdout), stderr_sha256=sha256(process.stderr))
                    if process.returncode != 0:
                        reason = "structural_unresolved" if process.returncode == 1 else "structural_execution_failed"
                        raise StructuralError(reason, "Mergiraf did not produce a complete conflict-free merge")
                    if set(item.name for item in output.iterdir()) != {"merged.py"} or destination.is_symlink() or not destination.is_file():
                        raise StructuralError("structural_invalid_output", "Structural output is not a single regular result file")
                    if destination.stat().st_size > MAX_OUTPUT_BYTES:
                        raise StructuralError("structural_output_too_large", "Structural result exceeds its size limit")
                    merged = destination.read_bytes()
                    output_size += len(merged)
                    if output_size > MAX_TOTAL_OUTPUT_BYTES:
                        raise StructuralError("structural_output_too_large", "Combined structural results exceed their size limit")
                    if any(location.read_bytes() != value for location, value in zip(paths, sides.values())):
                        raise StructuralError("structural_input_changed", "Structural tool changed an input snapshot")
                    try:
                        text = merged.decode("utf-8")
                        if not text.strip() or "\0" in text or MARKERS.search(text):
                            raise ValueError()
                        ast.parse(text)
                    except (UnicodeError, SyntaxError, ValueError, RecursionError):
                        raise StructuralError("structural_invalid_output", "Structural result is not complete Python source") from None
                    record["output_sha256"] = sha256(merged)
                    files[path] = text
                metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)
                return {"files": files, "explanation": "Mergiraf объединил структуру; требуется проверка поведения.", "metadata": metadata}
        except StructuralError as error:
            metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)
            error.metadata = metadata
            raise
        except OSError:
            metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)
            raise StructuralError("structural_io_error", "Structural temporary files could not be processed", metadata) from None
