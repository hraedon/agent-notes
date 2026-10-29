from __future__ import annotations

import argparse
import json
from typing import Any

from agent_notes.cli.common import (
    EXIT_CONFLICT,
    EXIT_GENERIC,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    MemoryTarget,
    ScopeError,
    _add_common,
    _print_sub_help,
    add_scope_args,
    emit_error,
    report_resolution_failure,
    resolve_composed_scopes,
    resolve_memory_target,
)


def _target(args: argparse.Namespace, *, create: bool = False) -> MemoryTarget | int:
    """Resolve the memory scope, or report the failure and return its exit code."""
    use_json = getattr(args, "json", False)
    try:
        return resolve_memory_target(args, create=create)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code
    except ScopeError as exc:
        return emit_error(exc.code, str(exc), use_json=use_json, exit_code=exc.exit_code)


def _tag(row: dict, target: MemoryTarget) -> dict:
    return {**row, "scope": target.scope, "scope_key": target.scope_key}


def _scope_label(row: dict) -> str:
    return f"{row['scope']}:{row['scope_key']}"


def _dedup_scopes(args: argparse.Namespace, target: MemoryTarget, warnings: list[str]) -> list:
    """The target tier plus its parent tiers (repo > vm > global), most specific first."""
    from agent_notes.core.config import VmIdError, resolve_vm_id
    from agent_notes.core.db import SCOPE_GLOBAL, SCOPE_VM, find_scope_project
    from agent_notes.core.memory_model import ScopeRef

    refs: list = []
    if target.proj_id is not None:
        refs.append(ScopeRef(target.scope, target.scope_key, target.proj_id))
    if target.scope == "repo":
        try:
            vm_id = resolve_vm_id(getattr(args, "vm", None))
        except VmIdError as exc:
            warnings.append(f"dedup: vm scope skipped: {exc}")
        else:
            vm_proj = find_scope_project(target.ws_id, SCOPE_VM, vm_id)
            if vm_proj is not None:
                refs.append(ScopeRef("vm", vm_id, vm_proj.id))
    if target.scope in ("repo", "vm"):
        global_proj = find_scope_project(target.ws_id, SCOPE_GLOBAL)
        if global_proj is not None:
            refs.append(ScopeRef("global", "global", global_proj.id))
    return refs


def _candidate_line(c: dict) -> str:
    score = "  n/a" if c["score"] is None else f"{c['score']:.3f}"
    flag = "!" if c.get("blocking") else " "
    return (
        f" {flag} {score}  {c['name']} [{c['scope']}:{c['scope_key']}] "
        f"(type={c['memory_type']}, match={'+'.join(c['match'])})"
    )


def cmd_mem_add(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    if not args.body or not args.body.strip():
        return emit_error(
            "VALIDATION_FAILED",
            "--body must be a non-empty string",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )
    supersedes = getattr(args, "supersedes", None)
    related_names: list[str] = list(getattr(args, "related", None) or [])
    distinct = bool(getattr(args, "distinct", False))
    if distinct and (supersedes or related_names):
        return emit_error(
            "VALIDATION_FAILED",
            "--distinct cannot be combined with --supersedes or --related",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )
    # Resolve without creating the tier row: a refused add must write nothing.
    target = _target(args)
    if isinstance(target, int):
        return target

    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import (
        DUPLICATE_GATE_SIMILARITY,
        add_memory,
        dedup_candidates,
        find_active,
        size_warning,
    )

    def resolve_choice(spec: str, refs: list) -> tuple[Any, dict] | None:
        """The row a --related/--supersedes value names, most specific tier first.

        A memory whose name is literally ``spec`` wins; only when there is none
        does a ``repo:``/``vm:``/``global:`` prefix select a tier for the rest.
        """

        def lookup(name: str, among: list) -> tuple[Any, dict] | None:
            for ref in among:
                if name == args.name and ref.project_id == target.proj_id:
                    continue  # the row this add revises is not a choice
                row = find_active(target.ws_id, ref.project_id, name)
                if row is not None:
                    return ref, row
            return None

        hit = lookup(spec, refs)
        tier, _, bare = spec.partition(":")
        if hit is None and bare and tier in ("repo", "vm", "global"):
            hit = lookup(bare, [r for r in refs if r.scope == tier])
        return hit

    from agent_notes.core.memory_model import ScopeRef

    answered: set[int] = set()
    if supersedes:
        # --supersedes acts only within the tier written to.
        own = (
            [ScopeRef(target.scope, target.scope_key, target.proj_id)]
            if target.proj_id is not None
            else []
        )
        if supersedes == args.name:
            supersedes = None  # re-adding a name already supersedes it
        else:
            sup_hit = resolve_choice(supersedes, own)
            if sup_hit is None:
                return emit_error(
                    "NOT_FOUND",
                    f"--supersedes: no active memory '{supersedes}' in "
                    f"{target.scope}:{target.scope_key} (supersede only within the tier you "
                    "write to)",
                    use_json=use_json,
                    exit_code=EXIT_NOT_FOUND,
                )
            answered.add(sup_hit[1]["id"])
            supersedes = sup_hit[1]["name"]

    # Re-adding a name that is active in the target tier is a plain revision:
    # it supersedes that row and is never gated. It cannot also replace a
    # second memory (one `supersedes` per row).
    revision = (
        target.proj_id is not None
        and find_active(target.ws_id, target.proj_id, args.name) is not None
    )
    if revision and supersedes:
        return emit_error(
            "VALIDATION_FAILED",
            f"'{args.name}' already exists in {target.scope}:{target.scope_key}, so this add "
            f"revises it; --supersedes {supersedes} cannot be combined with a revision. "
            f"Supersede {supersedes} separately, or add under a new name.",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    vec = embed(args.body, task="document").tolist()
    attributes = json.loads(args.attributes) if args.attributes else {}

    # WI-1.1: the writing agent is the judge. Show the nearest facts in this
    # tier and its parents; above the threshold (or on a name this one would
    # shadow) require an explicit choice. Reflections are dated episodes and
    # skip the check; revisions see the candidates but are not blocked.
    warnings: list[str] = []
    gated = args.type != "reflection"
    scopes = _dedup_scopes(args, target, warnings) if gated or related_names else []
    candidates: list[dict] = (
        dedup_candidates(target.ws_id, vec, scopes, args.name, target.proj_id) if gated else []
    )
    for c in candidates:
        # Compare at full precision; round only for display.
        c["blocking"] = not revision and (
            "name" in c["match"]
            or (c["score"] is not None and c["score"] >= DUPLICATE_GATE_SIMILARITY)
        )
        if c["score"] is not None:
            c["score"] = round(c["score"], 4)

    # Each choice binds to one resolved row (see resolve_choice): --related
    # NAME takes the most specific tier holding NAME; TIER:NAME picks a tier
    # unless a memory is literally called TIER:NAME.
    related: list[tuple[int, str]] = []
    related_out: list[dict] = []
    for spec in related_names:
        hit = resolve_choice(spec, scopes)
        if hit is None:
            return emit_error(
                "NOT_FOUND",
                f"--related: no active memory '{spec}' in this tier or its parent tiers",
                use_json=use_json,
                exit_code=EXIT_NOT_FOUND,
            )
        ref, row = hit
        answered.add(row["id"])
        related.append((ref.project_id, row["name"]))
        related_out.append({"name": row["name"], "scope": ref.scope, "scope_key": ref.scope_key})

    unanswered = [c for c in candidates if c["blocking"] and c["id"] not in answered]
    if unanswered and not distinct:
        names = ", ".join(f"{c['name']} [{c['scope']}:{c['scope_key']}]" for c in unanswered)
        message = (
            f"possible duplicate of {names}: re-run with --supersedes NAME (replace it), "
            "--related [TIER:]NAME (keep both, linked) or --distinct (keep both, unrelated)"
        )
        if not use_json:
            import sys

            print("Nearest existing facts:", file=sys.stderr)
            for c in candidates:
                print(_candidate_line(c), file=sys.stderr)
            print(
                f"   (! = blocking: similarity >= {DUPLICATE_GATE_SIMILARITY} or same name "
                "in a parent tier)",
                file=sys.stderr,
            )
        return emit_error(
            "POSSIBLE_DUPLICATE",
            message,
            use_json=use_json,
            detail={
                "threshold": DUPLICATE_GATE_SIMILARITY,
                "blocking": [
                    {
                        "id": c["id"],
                        "name": c["name"],
                        "scope": c["scope"],
                        "scope_key": c["scope_key"],
                    }
                    for c in unanswered
                ],
                "candidates": candidates,
            }
            if use_json
            else None,
            exit_code=EXIT_CONFLICT,
        )

    if target.proj_id is None:
        target = _target(args, create=True)
        if isinstance(target, int):
            return target
    assert target.proj_id is not None
    try:
        mem = add_memory(
            workspace_id=target.ws_id,
            project_id=target.proj_id,
            name=args.name,
            memory_type=args.type,
            body=args.body,
            attributes=attributes,
            embedding=vec,
            supersedes_name=supersedes,
            related=related,
        )
    except ValueError as exc:
        return emit_error(
            "VALIDATION_FAILED",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    size = size_warning(args.type, args.body)
    if size:
        warnings.append(size)
    mem = _tag(mem, target)
    if use_json:
        payload: dict[str, Any] = {
            "memory": mem,
            "dedup": {
                "checked": gated,
                "threshold": DUPLICATE_GATE_SIMILARITY,
                "candidates": candidates,
                "revision": revision,
                "distinct": distinct,
                "related": related_out,
                "supersedes": supersedes,
            },
        }
        if warnings:
            payload["warnings"] = warnings
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_warnings(warnings)
        old_id = mem.get("supersedes")
        superseded_msg = f" (superseded id={old_id})" if old_id else ""
        mem_name = mem["name"]
        mem_id = mem["id"]
        mem_type = mem["memory_type"]
        where = "" if target.scope == "repo" else f" in {_scope_label(mem)}"
        print(f"Memory '{mem_name}' added{where} (id={mem_id}, type={mem_type}){superseded_msg}")
        if candidates:
            print("Nearest existing facts:")
            for c in candidates:
                print(_candidate_line(c))
    return EXIT_SUCCESS


def cmd_mem_get(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    target = _target(args)
    if isinstance(target, int):
        return target

    from agent_notes.core.memory_model import get_memory

    mem = get_memory(target.ws_id, target.proj_id, args.name) if target.proj_id else None
    if mem is None:
        where = "" if target.scope == "repo" else f" in {target.scope}:{target.scope_key}"
        return emit_error(
            "NOT_FOUND",
            f"Memory '{args.name}' not found{where}.",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    mem = _tag(mem, target)
    warnings = _attach_contradictions(target.ws_id, [(target.proj_id, mem["name"], mem)])
    if use_json:
        payload: dict[str, Any] = {"memory": mem}
        if warnings:
            payload["warnings"] = warnings
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_warnings(warnings)
        lines = [
            f"**{mem['name']}** (type={mem['memory_type']}, id={mem['id']})",
            f"Created: {mem['created_at']}",
            f"Updated: {mem['updated_at']}",
            "",
            mem["body"],
        ]
        print("\n".join(lines))
    return EXIT_SUCCESS


def _attach_contradictions(
    ws_id: int, rows: list[tuple[int | None, str | None, dict]]
) -> list[str]:
    """WI-1.2: set ``contradictions`` on each returned row; return one warning per row.

    ``rows`` are ``(project row id, memory name, result)``. Only results with
    an open contradiction get the key, so a clean result's shape is unchanged.
    """
    from agent_notes.core.memory_model import contradiction_warning, open_contradictions

    keyed = [(pid, name, row) for pid, name, row in rows if pid is not None and name]
    found = open_contradictions(ws_id, [(pid, name) for pid, name, _ in keyed])
    warnings = []
    for pid, name, row in keyed:
        hits = found.get((pid, name))
        if hits:
            row["contradictions"] = hits
            warnings.append(contradiction_warning(name, hits))
    return warnings


def _composed_target(
    args: argparse.Namespace,
) -> tuple[Any, list, list[str]] | int:
    """Composed-scope resolution with the shared error reporting."""
    try:
        return resolve_composed_scopes(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
        report_resolution_failure(args, code)
        return code
    except ScopeError as exc:
        return emit_error(
            exc.code, str(exc), use_json=getattr(args, "json", False), exit_code=exc.exit_code
        )


def _scopes_payload(refs: list) -> list[dict]:
    return [{"scope": r.scope, "scope_key": r.scope_key} for r in refs]


def _print_warnings(warnings: list[str]) -> None:
    import sys

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)


def _print_shadows(row: dict) -> None:
    for sh in row.get("shadows") or []:
        preview = (sh.get("body_preview") or "").replace("\n", " ")
        print(f"  shadows {sh['scope']}:{sh['scope_key']}: {preview}")


def cmd_mem_list(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    limit = min(args.limit or 50, 200)
    scope = getattr(args, "scope", "repo")

    from agent_notes.core.memory_model import list_memories, list_memories_composed

    extra: dict[str, Any] = {}
    if scope == "composed":
        resolved = _composed_target(args)
        if isinstance(resolved, int):
            return resolved
        ctx, refs, warnings = resolved
        rows = list_memories_composed(ctx.ws_id, refs, memory_type=args.type, limit=limit)
        extra["scopes"] = _scopes_payload(refs)
        if warnings:
            extra["warnings"] = warnings
            if not use_json:
                _print_warnings(warnings)
    else:
        target = _target(args)
        if isinstance(target, int):
            return target
        rows = (
            list_memories(
                workspace_id=target.ws_id,
                project_id=target.proj_id,
                memory_type=args.type,
                limit=limit,
            )
            if target.proj_id
            else []
        )
        rows = [_tag(r, target) for r in rows]

    if use_json:
        print(json.dumps({"memories": rows, **extra}, indent=2, default=str))
    else:
        if not rows:
            print("No memories found.")
        else:
            print(f"{len(rows)} memory(ies):")
            for r in rows:
                preview = (r.get("body_preview") or "").replace("\n", " ")[:80]
                tag = "" if scope == "repo" else f" [{_scope_label(r)}]"
                print(f"- **{r['name']}**{tag} (type={r['memory_type']}) {preview}")
                _print_shadows(r)
    return EXIT_SUCCESS


def cmd_mem_search(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    limit = min(args.limit or 10, 50)
    scope = getattr(args, "scope", "composed")
    reflections = getattr(args, "reflections", None) or "demote"

    from agent_notes.core.embed import embed
    from agent_notes.core.memory_model import search_memory_composed, search_memory_with_body

    extra: dict[str, Any] = {}
    warnings: list[str] = []
    if scope == "composed":
        resolved = _composed_target(args)
        if isinstance(resolved, int):
            return resolved
        ctx, refs, warnings = resolved
        ws_id = ctx.ws_id
        vec = embed(args.query, task="query").tolist()
        rows = search_memory_composed(ws_id, vec, refs, limit=limit, reflections=reflections)
        extra["scopes"] = _scopes_payload(refs)
        keyed = [(r.get("project_id"), r["name"], r) for r in rows]
    else:
        target = _target(args)
        if isinstance(target, int):
            return target
        ws_id = target.ws_id
        vec = embed(args.query, task="query").tolist()
        rows = (
            search_memory_with_body(
                workspace_id=target.ws_id,
                query_vec=vec,
                project_id=target.proj_id,
                limit=limit,
                reflections=reflections,
            )
            if target.proj_id
            else []
        )
        rows = [_tag(r, target) for r in rows]
        keyed = [(target.proj_id, r["name"], r) for r in rows]
    warnings = [*warnings, *_attach_contradictions(ws_id, keyed)]
    extra["reflections"] = reflections
    if warnings:
        extra["warnings"] = warnings

    if use_json:
        print(json.dumps({"memories": rows, **extra}, indent=2, default=str))
    else:
        _print_warnings(warnings)
        if not rows:
            print("No matching memories.")
        else:
            print(f"{len(rows)} memory(ies) matched:")
            for r in rows:
                tag = "" if scope == "repo" else f" [{_scope_label(r)}]"
                print(f"- **{r['name']}**{tag} (type={r['memory_type']}, score={r['score']:.3f})")
                if r.get("contradictions"):
                    print("  ! open contradiction: see warning above")
                if r.get("body"):
                    print(f"  {r['body'][:200]}")
                _print_shadows(r)
    return EXIT_SUCCESS


def cmd_mem_recall(args: argparse.Namespace) -> int:
    """Recall from the native memory engine across one or all memory tiers."""
    use_json = getattr(args, "json", False)
    scope = getattr(args, "scope", "composed")
    reflections = getattr(args, "reflections", None) or "demote"

    from agent_notes.core.db import vm_project_slug
    from agent_notes.core.memory_engine import MemoryScope, RecallQuery, get_engine
    from agent_notes.core.memory_model import rank_key

    # (scope, scope_key, engine project slug, project row id), most specific first.
    tiers: list[tuple[str, str, str, int | None]] = []
    warnings: list[str] = []
    refs: list = []
    if scope == "composed":
        resolved = _composed_target(args)
        if isinstance(resolved, int):
            return resolved
        ctx, refs, warnings = resolved
        ws_slug, ws_id = ctx.ws_slug, ctx.ws_id
        for r in refs:
            slug = vm_project_slug(r.scope_key) if r.scope == "vm" else r.scope_key
            tiers.append((r.scope, r.scope_key, slug, r.project_id))
    else:
        target = _target(args)
        if isinstance(target, int):
            return target
        ws_slug, ws_id = target.ws_slug, target.ws_id
        if target.proj_id:
            tiers.append((target.scope, target.scope_key, target.proj_slug, target.proj_id))

    try:
        engine = get_engine()
    except ValueError as exc:
        return emit_error("ENGINE_UNAVAILABLE", str(exc), use_json=use_json, exit_code=EXIT_GENERIC)

    limit = min(args.limit or 10, 50)
    merged: list[dict] = []
    for rank, (tier, key, slug, _pid) in enumerate(tiers):
        try:
            response = engine.recall(
                RecallQuery(
                    query=args.query,
                    scope=MemoryScope(project_slug=slug, workspace_slug=ws_slug),
                    budget=args.budget,
                    max_tokens=4096,
                    reflections=reflections,
                )
            )
        except Exception as exc:
            warnings.append(f"{tier}:{key} recall failed: {type(exc).__name__}")
            continue
        if response.usage.get("error"):
            warnings.append(f"{tier}:{key} recall degraded: {response.usage['error']}")
            continue
        for res in response.results:
            merged.append({**res.to_dict(), "scope": tier, "scope_key": key, "_rank": rank})

    # A more specific tier's memory can fall below its own recall cutoff while
    # a less specific one with the same name is returned: look the names up
    # exactly and let it take that name's place (and score).
    if refs:
        from agent_notes.core.memory_model import active_by_name

        best: dict[str, dict] = {}
        for r in merged:
            ref = r.get("source_ref")
            if ref and (ref not in best or r["score"] > best[ref]["score"]):
                best[ref] = r
        returned = {(r.get("source_ref"), r["scope"], r["scope_key"]) for r in merged}
        rank_of = {(t, k): i for i, (t, k, _, _) in enumerate(tiers)}
        for row in active_by_name(ctx.ws_id, refs, list(best)):
            # The recovered row obeys the same reflection policy as recall
            # itself: under `exclude`, a repo reflection never replaces (or
            # shadows) a fact of the same name from a parent tier.
            if reflections == "exclude" and row["memory_type"] == "reflection":
                continue
            name, rank = row["name"], rank_of[(row["scope"], row["scope_key"])]
            top = best[name]
            if rank < top["_rank"] and (name, row["scope"], row["scope_key"]) not in returned:
                merged.append(
                    {
                        **top,
                        "text": row["body"],
                        "memory_type": row["memory_type"],
                        "scope": row["scope"],
                        "scope_key": row["scope_key"],
                        "_rank": rank,
                    }
                )
                returned.add((name, row["scope"], row["scope_key"]))

    # Rank by score with no tier boost (reflections demoted per WI-1.3), then
    # let the most specific tier win a name collision (source_ref is the
    # memory name for exact-source results).
    merged.sort(key=lambda r: rank_key(r, reflections), reverse=True)
    winners: dict[str, dict] = {}
    for r in merged:
        ref = r.get("source_ref")
        if ref and (ref not in winners or r["_rank"] < winners[ref]["_rank"]):
            winners[ref] = r
    results: list[dict] = []
    for r in merged:
        ref = r.get("source_ref")
        if ref and winners[ref] is not r:
            continue
        r["shadows"] = [
            {
                "scope": o["scope"],
                "scope_key": o["scope_key"],
                "body_preview": (o["text"] or "")[:120],
            }
            for o in merged
            if ref and o is not r and o.get("source_ref") == ref
        ]
        results.append(r)
    results = results[:limit]
    pid_of = {(t, k): pid for t, k, _, pid in tiers}
    for r in results:
        r.pop("_rank", None)
    warnings = [
        *warnings,
        *_attach_contradictions(
            ws_id,
            [(pid_of.get((r["scope"], r["scope_key"])), r.get("source_ref"), r) for r in results],
        ),
    ]

    if use_json:
        payload: dict[str, Any] = {
            "engine": engine.engine_name,
            "results": results,
            "scopes": [{"scope": t, "scope_key": k} for t, k, _, _ in tiers],
            "reflections": reflections,
        }
        if warnings:
            payload["warnings"] = warnings
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_warnings(warnings)
        if not results:
            print("No recalled memories.")
        else:
            print(f"{len(results)} recalled ({engine.engine_name}):")
            for r in results:
                name = r.get("source_ref") or "(synthesized)"
                print(
                    f"- **{name}** [{_scope_label(r)}] "
                    f"({r['origin']}, score={r['score']:.3f}) {r['text'][:200]}"
                )
                _print_shadows(r)
    return EXIT_SUCCESS


def cmd_mem_delete(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    target = _target(args)
    if isinstance(target, int):
        return target

    from agent_notes.core.memory_model import delete_memory

    row = delete_memory(target.ws_id, target.proj_id, args.name) if target.proj_id else None
    if row is None:
        return emit_error(
            "NOT_FOUND",
            f"Memory '{args.name}' not found (or already deleted).",
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )
    if use_json:
        print(
            json.dumps(
                {
                    "deleted": args.name,
                    "id": row["id"],
                    "scope": target.scope,
                    "scope_key": target.scope_key,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(f"Memory '{args.name}' soft-deleted (id={row['id']}).")
    return EXIT_SUCCESS


def cmd_mem_update(args: argparse.Namespace) -> int:
    use_json = getattr(args, "json", False)
    target = _target(args)
    if isinstance(target, int):
        return target

    from agent_notes.core.memory_model import size_warning, update_memory

    if args.body is None and args.attributes is None:
        msg = "At least one of --body or --attributes is required"
        return emit_error(
            "VALIDATION_FAILED",
            msg,
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )

    attributes = json.loads(args.attributes) if args.attributes else None
    try:
        if target.proj_id is None:
            raise ValueError(f"Memory '{args.name}' not found (or deleted)")
        mem = update_memory(
            workspace_id=target.ws_id,
            project_id=target.proj_id,
            name=args.name,
            body=args.body,
            attributes=attributes,
        )
    except ValueError as exc:
        return emit_error(
            "NOT_FOUND",
            str(exc),
            use_json=use_json,
            exit_code=EXIT_NOT_FOUND,
        )

    mem = _tag(mem, target)
    size = size_warning(mem.get("memory_type"), args.body)
    warnings = [size] if size else []
    if use_json:
        payload: dict[str, Any] = {"memory": mem}
        if warnings:
            payload["warnings"] = warnings
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_warnings(warnings)
        print(f"Memory '{mem['name']}' updated (id={mem['id']}).")
    return EXIT_SUCCESS


def _link_ends(args: argparse.Namespace) -> tuple[MemoryTarget, MemoryTarget] | int:
    """Resolve ``memory link``'s two ends: FROM in --scope, TO in --to-scope."""
    source = _target(args)
    if isinstance(source, int):
        return source
    to_scope = getattr(args, "to_scope", None) or source.scope
    if to_scope == source.scope:
        return source, source
    to_args = argparse.Namespace(**vars(args))
    to_args.scope = to_scope
    if to_scope != "repo":
        to_args.project = None
    elif source.scope != "repo":
        to_args.vm = None
    dest = _target(to_args)
    if isinstance(dest, int):
        return dest
    return source, dest


def _cmd_mem_link(args: argparse.Namespace, *, remove: bool) -> int:
    use_json = getattr(args, "json", False)
    ends = _link_ends(args)
    if isinstance(ends, int):
        return ends
    source, dest = ends

    from agent_notes.core.links import add_link, remove_link
    from agent_notes.core.memory_model import find_active

    if (source.proj_id, args.from_name) == (dest.proj_id, args.to_name):
        return emit_error(
            "VALIDATION_FAILED",
            "a memory cannot be linked to itself",
            use_json=use_json,
            exit_code=EXIT_CONFLICT,
        )
    if not remove:
        for end, name in ((source, args.from_name), (dest, args.to_name)):
            if end.proj_id is None or find_active(end.ws_id, end.proj_id, name) is None:
                return emit_error(
                    "NOT_FOUND",
                    f"Memory '{name}' not found in {end.scope}:{end.scope_key}.",
                    use_json=use_json,
                    exit_code=EXIT_NOT_FOUND,
                )
    link = {
        "from_kind": "memory",
        "from_workspace": source.ws_id,
        "from_project": source.proj_id,
        "from_identifier": args.from_name,
        "to_kind": "memory",
        "to_workspace": dest.ws_id,
        "to_project": dest.proj_id,
        "to_identifier": args.to_name,
        "relationship": args.kind,
    }
    ok = True
    if remove:
        ok = source.proj_id is not None and dest.proj_id is not None and remove_link(**link)
        if not ok:
            return emit_error(
                "NOT_FOUND",
                f"No {args.kind} link from '{args.from_name}' to '{args.to_name}'.",
                use_json=use_json,
                exit_code=EXIT_NOT_FOUND,
            )
    else:
        add_link(**link)

    result = {
        "from": {"name": args.from_name, "scope": source.scope, "scope_key": source.scope_key},
        "to": {"name": args.to_name, "scope": dest.scope, "scope_key": dest.scope_key},
        "kind": args.kind,
        "removed" if remove else "linked": True,
    }
    if use_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        verb = "Unlinked" if remove else "Linked"
        print(
            f"{verb}: {args.from_name} [{source.scope}:{source.scope_key}] --{args.kind}--> "
            f"{args.to_name} [{dest.scope}:{dest.scope_key}]"
        )
    return EXIT_SUCCESS


def cmd_mem_link(args: argparse.Namespace) -> int:
    """Link two memories (WI-1.2: ``--kind contradicts`` flags a conflict)."""
    return _cmd_mem_link(args, remove=False)


def cmd_mem_unlink(args: argparse.Namespace) -> int:
    """Remove a memory link; unlinking ``contradicts`` resolves the contradiction."""
    return _cmd_mem_link(args, remove=True)


def _print_hygiene(report: dict) -> None:
    def where(r: dict) -> str:
        return f"{r['name']} [{r['scope']}:{r['scope_key']}]"

    s = report["summary"]
    print(
        "memory hygiene: "
        + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in s.items())
        + f" ({report['elapsed_seconds']}s)"
    )
    if report["near_duplicates"]:
        sim = report["thresholds"]["duplicate_similarity"]
        print(f"\nNear-duplicate facts (similarity >= {sim}):")
        for d in report["near_duplicates"]:
            print(f"  {d['similarity']:.3f}  {where(d['a'])}  ~  {where(d['b'])}")
    if report["oversized"]:
        print(f"\nOversized fact bodies (> {report['thresholds']['size_guidance_chars']} chars):")
        for r in report["oversized"]:
            print(f"  {r['chars']:>7}  {where(r)} (type={r['memory_type']})")
    if report["shadowed"]:
        print("\nSame name in several tiers (the first hides the others):")
        for r in report["shadowed"]:
            hidden = ", ".join(f"{h['scope']}:{h['scope_key']}" for h in r["shadows"])
            print(f"  {where(r)} shadows {hidden}")
    if report["contradictions"]:
        print("\nOpen contradictions:")
        for c in report["contradictions"]:
            print(f"  {where(c['memory'])} contradicts {where(c['contradicts'])}")
    if report["stale"]:
        print(
            f"\nFacts not updated in {report['thresholds']['stale_days']} days that mention "
            "paths or versions:"
        )
        for r in report["stale"]:
            print(f"  {r['age_days']:>4}d  {where(r)} ({', '.join(r['mentions'])})")
    if report["reflections_without_followup"]:
        print("\nReflections with no later fact in their project:")
        for g in report["reflections_without_followup"]:
            print(
                f"  {g['count']:>4}  {g['scope']}:{g['scope_key']} "
                f"({str(g['oldest'])[:10]} .. {str(g['newest'])[:10]})"
            )
    if report["possible_secrets"]:
        print("\nPossible secrets (names and pattern types only; rotate, then supersede):")
        for r in report["possible_secrets"]:
            state = "" if r["active"] else " (inactive row)"
            patterns = ", ".join(f"{k} x{v}" for k, v in r["patterns"].items())
            print(f"  {where(r)} id={r['id']}{state}: {patterns}")


def cmd_mem_hygiene(args: argparse.Namespace) -> int:
    """WI-1.5: deterministic, read-only store hygiene report."""
    use_json = getattr(args, "json", False)
    project_id: int | None = None
    scope: dict[str, Any] = {"all": True}
    if getattr(args, "all", False):
        if args.project or args.path:
            return emit_error(
                "VALIDATION_FAILED",
                "--all cannot be combined with --project or --path",
                use_json=use_json,
                exit_code=EXIT_CONFLICT,
            )
    else:
        from agent_notes.cli.common import _resolve

        try:
            _ws_id, project_id, ws_slug, proj_slug = _resolve(
                args.workspace, args.project, args.path
            )
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_NOT_CONFIGURED
            report_resolution_failure(args, code)
            return code
        scope = {"all": False, "workspace": ws_slug, "project": proj_slug}

    from agent_notes.core.memory_hygiene import hygiene_report, redact_strings

    # The scope header names the workspace and project: redact it like the rest.
    report = {
        "scope": redact_strings(scope),
        **hygiene_report(project_id=project_id, stale_days=args.stale_days),
    }
    if use_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_hygiene(report)
    return EXIT_SUCCESS


def register_memory_parsers(sub: argparse._SubParsersAction) -> None:
    from agent_notes.core.memory_model import MEMORY_LINK_KINDS, REFLECTION_POLICIES

    reflections_help = (
        "How reflections (session episodes) rank: demote (default; below facts of "
        "similar score), mixed (similarity only), or exclude."
    )

    mem = sub.add_parser("memory", help="Memory operations")
    mem_sub = mem.add_subparsers(dest="mem_cmd")

    mem_add = mem_sub.add_parser("add", help="Add a memory")
    mem_add.add_argument("--name", required=True)
    mem_add.add_argument("--body", required=True)
    mem_add.add_argument("--type", required=True, dest="type")
    mem_add.add_argument("--attributes", default=None)
    mem_add.add_argument(
        "--supersedes",
        default=None,
        metavar="NAME",
        help="Replace the near-duplicate NAME in the same tier (it is deactivated)",
    )
    mem_add.add_argument(
        "--related",
        action="append",
        default=None,
        metavar="NAME",
        help="Keep both and link this memory to NAME (relates_to); repeatable",
    )
    mem_add.add_argument(
        "--distinct",
        action="store_true",
        help="Keep both: the near-duplicate candidates are not the same fact",
    )
    add_scope_args(mem_add, default="repo")
    _add_common(mem_add)
    mem_add.set_defaults(func=cmd_mem_add)

    mem_get = mem_sub.add_parser("get", help="Get a memory")
    mem_get.add_argument("name")
    add_scope_args(mem_get, default="repo")
    _add_common(mem_get)
    mem_get.set_defaults(func=cmd_mem_get)

    mem_list = mem_sub.add_parser("list", help="List memories")
    mem_list.add_argument("--type", default=None, dest="type")
    mem_list.add_argument("--limit", type=int, default=50)
    add_scope_args(mem_list, composed=True, default="repo")
    _add_common(mem_list)
    mem_list.set_defaults(func=cmd_mem_list)

    mem_search = mem_sub.add_parser("search", help="Search memories")
    mem_search.add_argument("query")
    mem_search.add_argument("--limit", type=int, default=10)
    mem_search.add_argument(
        "--reflections", choices=REFLECTION_POLICIES, default="demote", help=reflections_help
    )
    add_scope_args(mem_search, composed=True, default="composed")
    _add_common(mem_search)
    mem_search.set_defaults(func=cmd_mem_search)

    mem_recall = mem_sub.add_parser(
        "recall", help="Recall from the native memory engine across memory tiers"
    )
    mem_recall.add_argument("query")
    mem_recall.add_argument("--limit", type=int, default=10)
    mem_recall.add_argument("--budget", choices=["low", "mid", "high"], default="mid")
    mem_recall.add_argument(
        "--reflections", choices=REFLECTION_POLICIES, default="demote", help=reflections_help
    )
    add_scope_args(mem_recall, composed=True, default="composed")
    _add_common(mem_recall)
    mem_recall.set_defaults(func=cmd_mem_recall)

    mem_delete = mem_sub.add_parser("delete", help="Delete (soft) a memory")
    mem_delete.add_argument("name")
    add_scope_args(mem_delete, default="repo")
    _add_common(mem_delete)
    mem_delete.set_defaults(func=cmd_mem_delete)

    mem_update = mem_sub.add_parser("update", help="Update a memory in-place")
    mem_update.add_argument("name")
    mem_update.add_argument("--body", default=None, help="Replace body text")
    mem_update.add_argument("--attributes", default=None, help="Merge attributes (JSON)")
    add_scope_args(mem_update, default="repo")
    _add_common(mem_update)
    mem_update.set_defaults(func=cmd_mem_update)

    for verb, func, helptext in (
        ("link", cmd_mem_link, "Link two memories (e.g. --kind contradicts)"),
        ("unlink", cmd_mem_unlink, "Remove a memory link (resolves a contradiction)"),
    ):
        p = mem_sub.add_parser(verb, help=helptext)
        p.add_argument("from_name", metavar="FROM")
        p.add_argument("to_name", metavar="TO")
        p.add_argument("--kind", choices=MEMORY_LINK_KINDS, required=True)
        add_scope_args(p, default="repo")
        p.add_argument(
            "--to-scope",
            choices=["repo", "vm", "global"],
            default=None,
            help="Tier of TO (default: the same as --scope)",
        )
        _add_common(p)
        p.set_defaults(func=func)

    mem_hygiene = mem_sub.add_parser(
        "hygiene", help="Read-only store hygiene report (duplicates, size, staleness, secrets)"
    )
    mem_hygiene.add_argument(
        "--all", action="store_true", help="Every project and tier, not just this repo"
    )
    mem_hygiene.add_argument(
        "--stale-days",
        type=int,
        default=180,
        help="Report facts not updated for this many days (default 180)",
    )
    _add_common(mem_hygiene)
    mem_hygiene.set_defaults(func=cmd_mem_hygiene)

    mem.set_defaults(func=lambda args: _print_sub_help(mem))
