from ai_phone.pipeline import prompts


def test_system_prompt_is_fixed_part_then_call_section():
    fixed = prompts.fixed_instructions(None)
    s = prompts.system_prompt(fixed, "Ask whether they are open on Sunday.")
    assert s.startswith(prompts.PHONE_INSTRUCTIONS)
    assert s.endswith(prompts.CALL_SECTION_HEADER + "Ask whether they are open on Sunday.")


def test_empty_call_section_falls_back_to_the_default():
    fixed = prompts.fixed_instructions(None)
    assert prompts.system_prompt(fixed, None).endswith(prompts.DEFAULT_CALL_SECTION)
    assert prompts.system_prompt(fixed, "   ").endswith(prompts.DEFAULT_CALL_SECTION)


def test_host_line_belongs_to_the_fixed_part():
    fixed = prompts.fixed_instructions("You are running on mac (arm64).")
    assert fixed == prompts.PHONE_INSTRUCTIONS + " You are running on mac (arm64)."
    assert prompts.system_prompt(fixed, "x").index("mac (arm64)") < prompts.system_prompt(fixed, "x").index(prompts.CALL_SECTION_HEADER)


def test_instructions_mention_the_end_marker_and_the_probes_differ_only_after_the_header():
    assert prompts.END_MARKER in prompts.PHONE_INSTRUCTIONS
    a, b = prompts.cache_probe_sections()
    fixed = prompts.fixed_instructions(None)
    sa, sb = prompts.system_prompt(fixed, a), prompts.system_prompt(fixed, b)
    head = fixed + prompts.CALL_SECTION_HEADER
    assert sa.startswith(head) and sb.startswith(head) and sa != sb and a[0] != b[0]
