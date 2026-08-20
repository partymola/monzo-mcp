"""Tests for the container's storage and callback contract.

Nothing here runs the image. These are source-reading assertions about a seam
pytest cannot otherwise reach: the container's paths and callback interface are
set in the Dockerfile, mounted by the runtime argument in `server.json`, and
described in the README. All three have to agree, and a change to one of them
is invisible to every behavioural test.

What these do NOT check: that the running process actually receives the
environment. They assert the Dockerfile declares it. An ENTRYPOINT that
scrubbed the environment would pass here and fail in reality.
"""

import json
import posixpath
import re
import unittest

from monzo_mcp import config

REPO_ROOT = config._PACKAGE_ROOT
DOCKERFILE = REPO_ROOT / "Dockerfile"
SERVER_JSON = REPO_ROOT / "server.json"
README = REPO_ROOT / "README.md"

MOUNT_POINT = "/data"


def _final_stage_instructions():
    """Yield (instruction, argument) for the final build stage only.

    Earlier stages do not ship: an ENV in a builder stage is absent from the
    image, so flattening the whole file would accept a Dockerfile that sets
    nothing in the stage that runs.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", DOCKERFILE.read_text())
    parsed = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        instruction, _, argument = line.partition(" ")
        parsed.append((instruction.upper(), argument.strip()))

    last_from = max((i for i, (k, _) in enumerate(parsed) if k == "FROM"), default=-1)
    return parsed[last_from + 1 :]


def _dockerfile_env():
    """Both ENV forms: `ENV k=v [k2=v2 ...]` and the legacy `ENV k v`."""
    env = {}
    for instruction, argument in _final_stage_instructions():
        if instruction != "ENV":
            continue
        if "=" in argument:
            for pair in argument.split():
                key, sep, value = pair.partition("=")
                if sep:
                    env[key] = value
        else:
            key, _, value = argument.partition(" ")
            if value:
                env[key] = value.strip()
    return env


def _under(path, directory):
    """True if `path` is `directory` or genuinely inside it, after normalising.

    `posixpath` rather than `os.path`: these are paths inside a Linux image but
    the comparison runs on whatever host the suite is on, and `ntpath.normpath`
    rewrites them with backslash separators, so nothing contains anything. It
    has to normalise rather than compare path components, or a path that climbs
    back out with `..` counts as inside.
    """
    path = posixpath.normpath(path)
    directory = posixpath.normpath(directory)
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def _readme_code_lines():
    """Lines inside fenced code blocks, with continuations joined.

    Scoped to code blocks so prose may mention a command without the assertions
    below treating it as one a user is told to run.
    """
    lines, fenced = [], False
    for line in README.read_text().splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            lines.append(line)
    return re.sub(r"\\\s*\n\s*", " ", "\n".join(lines)).splitlines()


class TestTheContainmentPredicate(unittest.TestCase):
    """`_under` has edge cases that nothing else in this file reaches.

    Both call sites compare paths that are equal today, so an equality
    assertion carries them and the containment branch is never exercised: a
    predicate that answered anything at all would pass the rest of the file.
    The branch only starts mattering when a third path variable is added,
    which is what the loop around it exists for.
    """

    def test_a_path_inside_is_under(self):
        self.assertTrue(_under("/data/config", "/data"))

    def test_the_directory_itself_is_under(self):
        self.assertTrue(_under("/data", "/data"))

    def test_a_sibling_sharing_a_prefix_is_not(self):
        self.assertFalse(_under("/dataX/config", "/data"))

    def test_a_path_escaping_upwards_is_not(self):
        """Why this normalises rather than comparing path components."""
        self.assertFalse(_under("/data/../elsewhere", "/data"))
        self.assertFalse(_under("/data/../../etc", "/data"))

    def test_a_relative_path_is_not(self):
        self.assertFalse(_under("config", "/data"))


class TestTheContainerStoresDataOnAVolume(unittest.TestCase):
    def test_the_dockerfile_moves_both_paths_off_the_package_default(self):
        env = _dockerfile_env()
        self.assertEqual(env.get("MONZO_MCP_CONFIG_DIR"), "/data/config")
        self.assertEqual(env.get("MONZO_MCP_DB_PATH"), "/data/monzo.db")

    def test_both_paths_sit_under_the_declared_volume(self):
        volumes = set()
        for instruction, argument in _final_stage_instructions():
            if instruction != "VOLUME":
                continue
            try:
                parsed = json.loads(argument)
            except json.JSONDecodeError:
                parsed = [argument]
            volumes.update(parsed if isinstance(parsed, list) else [parsed])
        self.assertIn(MOUNT_POINT, volumes)

        for key, value in _dockerfile_env().items():
            if key.endswith(("_CONFIG_DIR", "_DB_PATH")):
                self.assertTrue(
                    any(_under(value, v) for v in volumes),
                    f"{key}={value} is not under any declared VOLUME {sorted(volumes)}",
                )

    def test_the_registry_entry_mounts_the_same_directory(self):
        package = json.loads(SERVER_JSON.read_text())["packages"][0]
        mounts = [
            argument["value"]
            for argument in package.get("runtimeArguments", [])
            if argument.get("name") == "-v"
        ]
        self.assertEqual(len(mounts), 1, "expected exactly one volume runtime argument")

        container_path = mounts[0].split(":")[1]
        self.assertEqual(container_path, MOUNT_POINT)
        for key, value in _dockerfile_env().items():
            if key.endswith(("_CONFIG_DIR", "_DB_PATH")):
                self.assertTrue(_under(value, container_path), f"{key}={value} is not mounted")

    def test_the_mount_is_not_silently_optional(self):
        # Without it the container starts and every tool reports it is not
        # configured, so a client that treats the argument as optional produces
        # a setup that looks fine and holds nothing.
        package = json.loads(SERVER_JSON.read_text())["packages"][0]
        mount = next(a for a in package["runtimeArguments"] if a.get("name") == "-v")
        self.assertTrue(mount.get("isRequired"))

    def test_every_documented_docker_run_mounts_the_volume(self):
        # The `auth` invocation matters most: run without the mount it writes
        # the token to an anonymous volume and loses it, which looks like a
        # successful setup until the next call.
        runs = [line for line in _readme_code_lines() if "docker run" in line]
        self.assertGreaterEqual(len(runs), 2, "expected an auth run and a server run")
        for line in runs:
            self.assertRegex(line.strip(), rf"-v\s+\S+:{MOUNT_POINT}\b")


class TestTheCallbackCanReachTheContainer(unittest.TestCase):
    # The source-install default is pinned by test_callback_host_defaults_to_loopback
    # in tests/test_config.py, which reloads under a cleared environment. Asserting
    # it here instead would read the live module and fail for anyone who happens to
    # have MONZO_MCP_CALLBACK_HOST exported.

    def test_the_image_binds_somewhere_a_published_port_can_reach(self):
        # A published port arrives on the container's bridge interface, which a
        # localhost-bound listener refuses - so `auth` would wait for a callback
        # that cannot be delivered, with no error and no timeout.
        self.assertEqual(_dockerfile_env().get("MONZO_MCP_CALLBACK_HOST"), "0.0.0.0")

    def test_the_documented_publish_keeps_the_listener_off_the_network(self):
        # 0.0.0.0 inside the container is only safe because the host side of
        # the published port is pinned to loopback.
        port = config.MONZO_CALLBACK_PORT
        published = [line for line in _readme_code_lines() if re.search(rf"-p\s+\S*{port}", line)]
        self.assertTrue(published, "the README documents no published callback port")
        for line in published:
            self.assertRegex(line, rf"-p\s+127\.0\.0\.1:\d+:{port}\b")


if __name__ == "__main__":
    unittest.main()
