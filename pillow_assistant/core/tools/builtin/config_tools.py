"""Self-configuration tools: the Agent can manage model configs, assign models
to purpose roles (chat / vision / asr / compression) and switch the app language — all on the
user's request, persisted across restarts."""

from __future__ import annotations

from pillow_assistant.core import i18n
from pillow_assistant.core.i18n import t
from pillow_assistant.core.model_roles import assign, load_roles
from pillow_assistant.core.tools.base import Permission, ToolContext, ToolResult


class ListModelsTool:
    name = "list_models"
    permission = Permission.READONLY
    description = t("tool.lm.desc")
    parameters = {"type": "object", "properties": {}}

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        storage = getattr(ctx, "storage", None)
        # list_model_configs() yields sqlite3.Row (no .get); convert to dict.
        configs = [dict(c) for c in storage.list_model_configs()] if storage is not None else []
        lines: list[str] = []
        if not configs:
            lines.append(t("tool.lm.none"))
        else:
            lines.append(t("tool.lm.header_models"))
            for c in configs:
                base = c.get("base_url") or ""
                lines.append(f'- {c["display_name"]}: {c.get("provider", "")} / '
                             f'{c.get("model", "")} [{c.get("model_type", "")}] {base}'.rstrip())
        roles = load_roles()
        lines.append("")
        lines.append(t("tool.lm.header_roles"))
        for role in ("chat", "vision", "compression"):
            lines.append(f"- {role}: {roles.get(role) or t('tool.lm.unset')}")
        asr_pref = roles.get("asr")
        lines.append(f"- asr: {asr_pref if asr_pref else t('tool.lm.unset')}")
        try:
            from pillow_assistant.core import asr
            lines.append(t("tool.lm.asr_now", v=asr.backend() or "-"))
        except Exception:
            pass
        return ToolResult(ok=True, text="\n".join(lines))


class ConfigureModelTool:
    name = "configure_model"
    permission = Permission.SYSTEM
    description = t("tool.cm.desc")
    parameters = {
        "type": "object",
        "properties": {
            "display_name": {"type": "string", "description": t("tool.cm.display_name")},
            "provider": {"type": "string", "description": t("tool.cm.provider")},
            "model": {"type": "string", "description": t("tool.cm.model")},
            "base_url": {"type": "string", "description": t("tool.cm.base_url")},
            "api_key": {"type": "string", "description": t("tool.cm.api_key")},
            "model_type": {"type": "string", "enum": ["llm", "vlm"],
                           "description": t("tool.cm.model_type")},
        },
        "required": ["display_name", "model"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        storage = getattr(ctx, "storage", None)
        if storage is None:
            return ToolResult(ok=False, text=t("tool.cm.no_storage"))
        display_name = (args.get("display_name") or "").strip()
        model = (args.get("model") or "").strip()
        if not display_name or not model:
            return ToolResult(ok=False, text=t("tool.cm.missing"))
        provider = (args.get("provider") or "OpenAI").strip()
        model_type = (args.get("model_type") or "llm").strip().lower()
        if model_type not in ("llm", "vlm"):
            model_type = "llm"
        vault = getattr(ctx, "vault", None)

        # Hydrate existing configs' keys from the vault so a full replace
        # doesn't drop them (same flow as the config dialog).
        configs = [dict(r) for r in storage.list_model_configs()]
        for cfg in configs:
            cfg.setdefault("model", "")
            if vault is not None:
                cfg["api_key"] = vault.get_secret(cfg.get("display_name", "")) or ""

        entry = {
            "provider": provider, "model_type": model_type, "display_name": display_name,
            "model": model, "base_url": (args.get("base_url") or "").strip() or None,
            "api_key": (args.get("api_key") or "").strip(), "extra": "",
        }
        replaced = False
        for i, cfg in enumerate(configs):
            if cfg.get("display_name") == display_name:
                if not entry["api_key"]:
                    entry["api_key"] = cfg.get("api_key", "")
                entry["extra"] = cfg.get("extra", "") or ""
                configs[i] = entry
                replaced = True
                break
        if not replaced:
            configs.append(entry)
        storage.replace_model_configs(configs, vault)
        return ToolResult(ok=True, text=t("tool.cm.saved", name=display_name,
                                          provider=provider, model=model, type=model_type))


class AssignModelRoleTool:
    name = "assign_model_role"
    permission = Permission.SYSTEM
    description = t("tool.ar.desc")
    parameters = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ["chat", "vision", "asr", "compression"],
                     "description": t("tool.ar.role")},
            "model": {"type": "string", "description": t("tool.ar.model")},
            "whisper_size": {"type": "string", "description": t("tool.ar.size")},
        },
        "required": ["role", "model"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        role = (args.get("role") or "").strip().lower()
        value = (args.get("model") or "").strip()
        if role not in ("chat", "vision", "asr", "compression"):
            return ToolResult(ok=False, text=t("tool.ar.bad_role", role=role))

        if role == "asr":
            backend = value.lower()
            if backend not in ("sensevoice", "whisper"):
                return ToolResult(ok=False, text=t("tool.ar.bad_backend", name=value))
            entry: dict = {"backend": backend}
            size = (args.get("whisper_size") or "").strip()
            if size:
                entry["model"] = size
            assign("asr", entry)
            try:  # forget cached backend/models so the change applies now
                from pillow_assistant.core import asr
                asr.reset_cache()
            except Exception:
                pass
            return ToolResult(ok=True, text=t("tool.ar.saved", role=role, value=entry))

        storage = getattr(ctx, "storage", None)
        names = [c["display_name"] for c in storage.list_model_configs()] if storage is not None else []
        if value not in names:
            return ToolResult(ok=False, text=t("tool.ar.model_not_found", name=value))
        assign(role, value)
        return ToolResult(ok=True, text=t("tool.ar.saved", role=role, value=value))


class SetMaxStepsTool:
    name = "set_max_steps"
    permission = Permission.SYSTEM
    description = t("tool.steps.desc")
    parameters = {
        "type": "object",
        "properties": {"steps": {"type": "integer", "description": t("tool.steps.steps")}},
        "required": ["steps"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            steps = int(args.get("steps"))
        except (TypeError, ValueError):
            return ToolResult(ok=False, text=t("tool.steps.bad", v=args.get("steps")))
        if not (1 <= steps <= 500):
            return ToolResult(ok=False, text=t("tool.steps.bad", v=steps))
        from pillow_assistant.core.settings import set_setting
        set_setting("max_steps", steps)
        return ToolResult(ok=True, text=t("tool.steps.done", n=steps))


class SetSurfaceTransparencyTool:
    name = "set_surface_transparency"
    permission = Permission.SYSTEM
    description = (
        "Adjust the frosted-glass background. Opacity and transparency are exact "
        "complements: opacity + transparency = 100. Opacity 100 is fully opaque; "
        "opacity 0 is fully transparent. Transparency 100 is fully transparent; "
        "transparency 0 is fully opaque. For relative wording such as 'more transparent', "
        "use mode='more_transparent'; for 'more opaque', use mode='less_transparent'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["set", "more_transparent", "less_transparent"],
                "description": "Use set for an explicit opacity or transparency percentage.",
            },
            "transparency": {
                "type": "integer", "minimum": 0, "maximum": 100,
                "description": "Only for mode=set. 0 is fully opaque; 100 is fully transparent.",
            },
            "opacity": {
                "type": "integer", "minimum": 0, "maximum": 100,
                "description": "Only for mode=set. 0 is fully transparent; 100 is fully opaque.",
            },
            "amount": {
                "type": "integer", "minimum": 1, "maximum": 40,
                "description": "Relative adjustment size, default 15 percentage points.",
            },
        },
        "required": ["mode"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        mode = (args.get("mode") or "").strip().lower()
        from pillow_assistant.core.settings import load_settings, set_setting
        try:
            current_opacity = max(0, min(100, int(load_settings().get("surface_glass_opacity", 68))))
        except (TypeError, ValueError):
            current_opacity = 68
        current_transparency = 100 - current_opacity

        if mode == "set":
            has_transparency = args.get("transparency") is not None
            has_opacity = args.get("opacity") is not None
            if has_transparency == has_opacity:
                return ToolResult(
                    ok=False,
                    text="mode=set requires exactly one of transparency or opacity, from 0 to 100.",
                )
            try:
                if has_transparency:
                    transparency = int(args["transparency"])
                else:
                    opacity = int(args["opacity"])
                    transparency = 100 - opacity
            except (TypeError, ValueError):
                return ToolResult(ok=False, text="The percentage must be an integer from 0 to 100.")
            if not 0 <= transparency <= 100:
                return ToolResult(ok=False, text="The percentage must be from 0 to 100.")
        elif mode in ("more_transparent", "less_transparent"):
            try:
                amount = int(args.get("amount", 15))
            except (TypeError, ValueError):
                amount = 15
            amount = max(1, min(40, amount))
            if mode == "more_transparent":
                transparency = current_transparency + amount
            else:
                transparency = current_transparency - amount
        else:
            return ToolResult(ok=False, text="mode must be set, more_transparent, or less_transparent.")

        transparency = max(0, min(100, transparency))
        opacity = 100 - transparency
        set_setting("surface_glass_opacity", opacity)
        from pillow_assistant.ui.acrylic import notify_glass_opacity
        notify_glass_opacity(opacity)
        return ToolResult(
            ok=True,
            text=(f"Frosted-glass transparency is now {transparency}% "
                  f"(background opacity {opacity}%). Open display windows updated."),
        )

class SetLanguageTool:
    name = "set_language"
    permission = Permission.SYSTEM
    description = t("tool.lang.desc")
    parameters = {
        "type": "object",
        "properties": {"lang": {"type": "string", "enum": ["zh", "en"],
                                "description": t("tool.lang.lang")}},
        "required": ["lang"],
    }

    async def __call__(self, args: dict, ctx: ToolContext) -> ToolResult:
        lang = (args.get("lang") or "").strip().lower()
        if not i18n.set_language(lang):
            return ToolResult(ok=False, text=t("tool.lang.bad", lang=lang))
        return ToolResult(ok=True, text=t("tool.lang.done", lang=lang))
