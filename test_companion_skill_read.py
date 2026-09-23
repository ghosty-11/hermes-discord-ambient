"""companion_skill_view — a profile-opted-in, read-only skill seam.

Isolated proof, no live Hermes state touched:
  * the two retained skill bodies read by exact name from the CURRENT
    profile's own skills tree, returned as labelled reference data with no
    host path in the payload;
  * every other shape is denied uniformly — traversal, category-qualified
    and plugin-qualified handles, case/space variants, non-strings, an
    allowlisted name whose body is missing, oversized, symlinked (leaf or
    category), archived, or disabled in this profile's config;
  * other profiles without the per-profile opt-in (even with a mistaken
    toolset grant) and non-Discord surfaces never see the tool;
  * the resolved Discord schema carries exactly one custom view tool and
    none of the built-in skills_list/skill_view/skill_manage while the
    global `skills` veto holds, and cli/cron/api_server never resolve it.

The fixture checks the actual registered tool and surface-resolved schema.

Run standalone:
    HERMES_FRAMEWORK_PATH=<isolated framework checkout> \
        python3 test_companion_skill_read.py
"""

import importlib.util
import json
import os
import socket
import sys
import tempfile
from pathlib import Path


class _DeniedSocket(socket.socket):
    def connect(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")

    def connect_ex(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")


socket.socket = _DeniedSocket
os.environ["HOME"] = tempfile.mkdtemp(prefix="csr-skill-home-")
sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "companion_skill_read_test", HERE / "companion_skill_read.py")
csr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(csr)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


GRANT_CONFIG = """\
agent:
  disabled_toolsets:
    - terminal
    - skills
platform_toolsets:
  discord:
    - companion_skills
    - connections
    - gif
    - messaging
    - session_search
    - vision
    - web
  cli:
    - connections
    - messaging
    - session_search
    - vision
    - web
  cron:
    - connections
    - session_search
    - vision
    - web
  api_server:
    - connections
    - gif
    - messaging
    - session_search
    - vision
    - web
platforms:
  discord:
    extra:
      ambient_presence:
        enabled: true
        companion_skill_view: true
"""

NO_GRANT_CONFIG = GRANT_CONFIG.replace("    - companion_skills\n", "")
NO_PROFILE_OPT_IN = GRANT_CONFIG.replace(
    "companion_skill_view: true", "companion_skill_view: false"
)

DISABLED_CONFIG = GRANT_CONFIG + """\
skills:
  disabled:
    - optmem
"""

OPTMEM_BODY = "# optmem\n\nMEMORY PROCEDURE BODY MARKER m3m0ry.\n"
PHILOSOPHY_BODY = "# philosophy-of-agency\n\nAGENCY BODY MARKER 4g3ncy.\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _companion_home(root: Path, config: str) -> Path:
    """A named profile home with its own opt-in config."""
    home = root / "profiles" / "companion"
    _write(home / "config.yaml", config)
    return home


def _enter(home: Path, platform: str = "discord") -> Path:
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_PLATFORM"] = platform
    return home


def _leaks(payload_text: str, *roots: Path) -> bool:
    banned = [str(r) for r in roots] + ["/private/home"]
    return any(b and b in payload_text for b in banned)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="csr-skill-seam-"))

    # ---- fixture homes ----------------------------------------------------
    good_root = tmp / "good"
    companion = _companion_home(good_root, GRANT_CONFIG)
    _write(companion / "skills" / "memory" / "optmem" / "SKILL.md", OPTMEM_BODY)
    _write(companion / "skills" / "research" / "philosophy-of-agency" / "SKILL.md", PHILOSOPHY_BODY)
    # A stale archived copy must never be served for the live name.
    _write(companion / "skills" / ".archive" / "optmem" / "SKILL.md", "STALE ARCHIVE COPY c0rrupt\n")

    operator = good_root / "profiles" / "operator"
    _write(operator / "config.yaml", NO_GRANT_CONFIG)
    _write(operator / "skills" / "memory" / "optmem" / "SKILL.md",
           "# optmem\n\nOPERATOR PRIVATE COPY 4nub1s\n")

    # A toolset grant alone cannot opt another profile in.
    other_profile = good_root / "profiles" / "other_profile"
    _write(other_profile / "config.yaml", NO_PROFILE_OPT_IN)
    _write(other_profile / "skills" / "memory" / "optmem" / "SKILL.md",
           "# optmem\n\nOTHER PROFILE COPY th0th\n")

    missing_root = tmp / "missing"
    missing_companion = _companion_home(missing_root, GRANT_CONFIG)
    _write(missing_companion / "skills" / ".archive" / "optmem" / "SKILL.md", "ONLY ARCHIVED c0rrupt\n")
    _write(missing_companion / "skills" / "research" / "philosophy-of-agency" / "SKILL.md", PHILOSOPHY_BODY)

    symlink_root = tmp / "symlinked"
    symlink_companion = _companion_home(symlink_root, GRANT_CONFIG)
    _write(symlink_companion / "skills" / "research" / "philosophy-of-agency" / "SKILL.md", PHILOSOPHY_BODY)
    optmem_dir = symlink_companion / "skills" / "memory" / "optmem"
    optmem_dir.mkdir(parents=True)
    # Leaf symlink whose target is a legitimate in-profile body: still refused.
    (optmem_dir / "SKILL.md").symlink_to(
        symlink_companion / "skills" / "research" / "philosophy-of-agency" / "SKILL.md")
    # Category symlink pointing outside the profile home: never followed.
    outside = tmp / "outside"
    _write(outside / "optmem" / "SKILL.md", "OUTSIDE COPY 0uts1de\n")
    (symlink_companion / "skills" / "linked").symlink_to(outside, target_is_directory=True)

    disabled_root = tmp / "disabled"
    disabled_companion = _companion_home(disabled_root, DISABLED_CONFIG)
    _write(disabled_companion / "skills" / "memory" / "optmem" / "SKILL.md", OPTMEM_BODY)
    _write(disabled_companion / "skills" / "research" / "philosophy-of-agency" / "SKILL.md", PHILOSOPHY_BODY)

    cap = csr.MAX_SKILL_BODY_BYTES
    oversized_root = tmp / "oversized"
    oversized_companion = _companion_home(oversized_root, GRANT_CONFIG)
    _write(oversized_companion / "skills" / "memory" / "optmem" / "SKILL.md",
           "x" * (cap + 1))
    # Exactly at the cap: boundary is inclusive, must still serve.
    _write(oversized_companion / "skills" / "research" / "philosophy-of-agency" / "SKILL.md",
           ("4g3ncy " + "x" * cap)[:cap])

    nogrant_root = tmp / "nogrant"
    nogrant_companion = _companion_home(nogrant_root, NO_GRANT_CONFIG)
    _write(nogrant_companion / "skills" / "memory" / "optmem" / "SKILL.md", OPTMEM_BODY)

    # ---- schema shape -----------------------------------------------------
    schema = csr.COMPANION_SKILL_VIEW_SCHEMA
    check("schema names the registered tool", schema.get("name") == "companion_skill_view")
    check("schema exposes only the name argument",
          set(schema["parameters"]["properties"]) == {"name"},
          repr(sorted(schema["parameters"]["properties"])))
    check("schema enum is exactly the two retained skills",
          set(schema["parameters"]["properties"]["name"].get("enum", []))
          == {"optmem", "philosophy-of-agency"},
          repr(schema["parameters"]["properties"]["name"].get("enum")))

    # ---- positive reads on the current profile ----------------------------
    _enter(companion)
    check("check_fn passes on the opted-in companion discord surface",
          csr.companion_skills_available() is True)

    raw = csr.companion_skill_view_handler({"name": "optmem"})
    r = json.loads(raw)
    check("optmem body reads from the current profile",
          r.get("success") is True and "m3m0ry" in r.get("skill_data", {}).get("content", ""),
          repr(r)[:200])
    check("payload is provenance-labelled untrusted reference data",
          r.get("skill_data", {}).get("content_kind") == "untrusted_reference_text"
          and bool(r.get("skill_data", {}).get("provenance")))
    check("archived stale copy is not served for the live name",
          "c0rrupt" not in raw)
    check("no host path leaks into the model payload",
          not _leaks(raw, tmp, companion))

    raw2 = csr.companion_skill_view_handler({"name": "philosophy-of-agency"})
    r2 = json.loads(raw2)
    check("philosophy-of-agency body reads from the current profile",
          r2.get("success") is True and "4g3ncy" in r2.get("skill_data", {}).get("content", ""),
          repr(r2)[:200])
    check("skill_data names the skill that was asked for",
          r2.get("skill_data", {}).get("name") == "philosophy-of-agency")

    # ---- uniform denial of every other shape ------------------------------
    def denied(label, name_arg):
        out = csr.companion_skill_view_handler({"name": name_arg})
        try:
            payload = json.loads(out)
        except Exception:
            check(label, False, f"non-JSON refusal: {out[:120]!r}")
            return None
        check(label,
              payload.get("success") is False and not _leaks(out, tmp, companion),
              repr(payload)[:160])
        return payload.get("error")

    for bad in ("optmem/..", "../optmem", "memory/optmem", "skills/memory/optmem",
                "/etc/passwd", "optmem/SKILL.md", "./optmem", "optmem/.",
                "Optmem", "OPTMEM", " optmem", "optmem ", "optmem\n",
                "plugin:optmem", "optmem-tools", "philosophy_of_agency",
                "hermes-agent", "nonexistent", ""):
        denied(f"denied: {bad!r}", bad)
    denied("denied: non-string name (None)", None)
    denied("denied: non-string name (int)", 12345)
    check("denied: missing name argument",
          json.loads(csr.companion_skill_view_handler({})).get("success") is False)

    # ---- degraded trees: missing / symlinked / disabled / oversized -------
    _enter(missing_companion)
    err_missing = denied("denied: allowlisted skill missing from the profile tree", "optmem")
    check("positive control on the missing-skill home: the present skill still reads",
          json.loads(csr.companion_skill_view_handler({"name": "philosophy-of-agency"})
                     ).get("success") is True)
    check("archived copy does not satisfy a missing live skill", "c0rrupt" not in err_missing)

    _enter(symlink_companion)
    err_symlink = denied("denied: SKILL.md is a symlink to a legitimate in-profile body", "optmem")
    check("positive control on the symlink home: the regular skill still reads",
          json.loads(csr.companion_skill_view_handler({"name": "philosophy-of-agency"})
                     ).get("success") is True)
    check("symlinked outside category is never walked",
          "0uts1de" not in csr.companion_skill_view_handler({"name": "optmem"}))

    _enter(disabled_companion)
    err_disabled = denied("denied: skill disabled in this profile's config", "optmem")
    check("positive control on the disabled home: the other skill still reads",
          json.loads(csr.companion_skill_view_handler({"name": "philosophy-of-agency"})
                     ).get("success") is True)

    _enter(oversized_companion)
    err_oversize = denied("denied: skill body over the byte cap", "optmem")
    check("boundary: a body of exactly MAX_SKILL_BODY_BYTES still serves",
          json.loads(csr.companion_skill_view_handler({"name": "philosophy-of-agency"})
                     ).get("success") is True)

    check("missing/symlinked/disabled/oversized refusals are indistinguishable",
          len({err_missing, err_symlink, err_disabled, err_oversize}) == 1
          and None not in {err_missing, err_symlink, err_disabled, err_oversize},
          repr((err_missing, err_symlink, err_disabled, err_oversize)))

    # ---- profile identity, grant and surface gates ------------------------
    _enter(operator)
    check("check_fn hides the tool on another profile",
          csr.companion_skills_available() is False)
    out = csr.companion_skill_view_handler({"name": "optmem"})
    check("another profile's own skill copy is unreachable",
          json.loads(out).get("success") is False and "4nub1s" not in out)

    _enter(other_profile)
    check("profile opt-in is required even with the toolset grant",
          csr.companion_skills_available() is False)

    _enter(nogrant_companion)
    check("check_fn requires the explicit discord grant in this profile's config",
          csr.companion_skills_available() is False)

    for surface in ("cli", "cron", "api_server", None):
        if surface is None:
            os.environ.pop("HERMES_PLATFORM", None)
        else:
            _enter(companion, surface)
        label = surface or "unset"
        check(f"check_fn hides the tool off the discord surface ({label})",
              csr.companion_skills_available() is False)
        check(f"handler refuses off the discord surface ({label})",
              json.loads(csr.companion_skill_view_handler({"name": "optmem"})
                         ).get("success") is False)

    # ---- resolved schema (registered the way the gateway registers) -------
    _enter(companion)
    from hermes_cli.plugins import PluginContext, PluginManager  # noqa: E402
    from hermes_cli.plugins_manifest import parse_manifest_file  # noqa: E402
    import tools.skills_tool  # noqa: F401,E402  — registers the real built-in skills tools
    from hermes_cli.tools_config import _get_platform_tools  # noqa: E402
    from tools.registry import registry  # noqa: E402
    from toolsets import resolve_toolset  # noqa: E402
    import yaml  # noqa: E402

    # The REAL ambient manifest — the same parse the gateway loader performs
    # for this plugin. It must not declare provides_tools: that is the
    # CLI/TUI pre-declaration path and this tool is Discord-surface-only.
    manifest = parse_manifest_file(HERE / "plugin.yaml", HERE, source="user", prefix="")
    check("real ambient plugin.yaml parses as a platform plugin",
          manifest is not None and manifest.kind == "platform", repr(manifest))
    check("real ambient plugin.yaml declares no provides_tools",
          manifest is not None and not manifest.provides_tools,
          repr(getattr(manifest, "provides_tools", "unset")))
    if manifest is None:
        raise SystemExit("plugin.yaml did not parse; cannot run the schema phase")
    manager = PluginManager()  # scope key = live home
    csr.register_companion_skill_view(PluginContext(manifest, manager))
    check("register_companion_skill_view registers the tool under companion_skills",
          registry.get_entry("companion_skill_view") is not None
          and registry.get_toolset_for_tool("companion_skill_view") == "companion_skills")

    cfg = yaml.safe_load((companion / "config.yaml").read_text(encoding="utf-8"))
    discord_toolsets = _get_platform_tools(cfg, "discord")
    check("resolved discord surface keeps the explicit companion_skills grant",
          "companion_skills" in discord_toolsets, repr(sorted(discord_toolsets)))
    check("global skills veto holds on the resolved discord surface",
          "skills" not in discord_toolsets)

    def surface_names(surface):
        names = set()
        for ts in _get_platform_tools(cfg, surface):
            names |= set(resolve_toolset(ts))
        return names

    discord_names = surface_names("discord")
    defs = registry.get_definitions(discord_names)
    # get_definitions returns OpenAI wrappers: {"type": "function", "function": schema}.
    def_names = {d.get("function", {}).get("name") for d in defs}
    check("definitions carry the OpenAI function wrapper",
          bool(defs) and all(d.get("type") == "function"
                             and d.get("function", {}).get("name") for d in defs))
    # The schema may also carry other granted tools that registered in this
    # process (session_search, web_*...) — pre-existing tools are not this
    # seam's concern. The boundary that matters: the companion tool is
    # present, its toolset resolves to it alone, and no built-in skills
    # management tool reaches the schema while the global veto holds.
    check("resolved discord schema carries the custom view tool",
          "companion_skill_view" in def_names, repr(sorted(def_names)))
    check("the companion_skills toolset resolves to exactly the one custom tool",
          resolve_toolset("companion_skills") == ["companion_skill_view"],
          repr(resolve_toolset("companion_skills")))
    check("no built-in skills_list/skill_view/skill_manage reaches the schema",
          not ({"skills_list", "skill_view", "skill_manage"} & def_names)
          and not ({"skills_list", "skill_view", "skill_manage"} & discord_names),
          repr(sorted(discord_names)))

    for surface in ("cli", "cron", "api_server"):
        names_s = surface_names(surface)
        check(f"{surface} surface never resolves the companion tool",
              "companion_skill_view" not in names_s
              and "companion_skills" not in _get_platform_tools(cfg, surface),
              repr(sorted(names_s)))

    _enter(operator)
    check("other profile: the tool is absent from definitions even when named explicitly",
          registry.get_definitions({"companion_skill_view"}) == [])

    if FAILURES:
        raise SystemExit(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    print("companion_skill_view seam regression passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
