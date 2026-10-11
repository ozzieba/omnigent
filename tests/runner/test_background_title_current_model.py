"""Reached economy model dispatch without a native/provider invocation."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omnigent.runner.background_titles import service


class CurrentEconomyModelTests(unittest.TestCase):
    def test_current_luna_reaches_both_native_generators(self):
        for harness in ("codex", "codex-native"):
            seen = []

            async def generator(context, seen=seen):
                seen.append(context.title_model)
                return "Synthetic title"

            context = service.BackgroundTitleContext(
                prompt="synthetic", harness=harness, spawn_env={}, process_manager=object()
            )
            with (
                patch.object(
                    service,
                    "generator_spec_for_harness",
                    return_value=SimpleNamespace(generator="fake"),
                ),
                patch.object(service, "load_object", return_value=generator),
            ):
                self.assertEqual(
                    asyncio.run(service.generate_background_title(context)), "Synthetic title"
                )
            self.assertEqual(seen, ["gpt-6-luna"])

    def test_explicit_title_model_preserved(self):
        seen = []

        async def generator(context):
            seen.append(context.title_model)
            return "Synthetic title"

        context = service.BackgroundTitleContext(
            prompt="synthetic",
            harness="codex-native",
            spawn_env={},
            process_manager=object(),
            title_model="gpt-5-nano",
        )
        with (
            patch.object(
                service,
                "generator_spec_for_harness",
                return_value=SimpleNamespace(generator="fake"),
            ),
            patch.object(service, "load_object", return_value=generator),
        ):
            asyncio.run(service.generate_background_title(context))
        self.assertEqual(seen, ["gpt-5-nano"])

    def test_timeout_does_not_retry_on_expensive_session_model(self):
        seen = []

        async def generator(context):
            seen.append(context.title_model)
            raise TimeoutError()

        context = service.BackgroundTitleContext(
            prompt="synthetic",
            harness="codex-native",
            spawn_env={},
            process_manager=object(),
            model_override="gpt-6.1-sol",
        )
        with (
            patch.object(
                service,
                "generator_spec_for_harness",
                return_value=SimpleNamespace(generator="fake"),
            ),
            patch.object(service, "load_object", return_value=generator),
            self.assertRaises(TimeoutError),
        ):
            asyncio.run(service.generate_background_title(context))
        self.assertEqual(seen, ["gpt-6-luna"])
