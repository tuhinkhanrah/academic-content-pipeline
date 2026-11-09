You are planning a chapter-specific NEET UG question-bank allocation.

Target question count: {{target_questions}}. This is a target and a hard upper limit, not a required exact count.
Target standard: {{standards}}

Allowed question types and subject-profile weights:
{{type_profile}}

## Source chapter content: {{source_filename}}

{{chapter_content}}

## Allocation instructions

Choose the question types and counts that are genuinely supported by this chapter. Use the profile weights as preferences, not quotas. A type may receive zero questions when the chapter does not support good, distinct questions of that type. Do not invent content merely to fill the target. Prefer a smaller high-quality allocation over forced or repetitive questions.

Return only valid JSON in this exact shape:
{"allocations":[{"type":"standard_mcq","count":12}]}

Rules:
- Use only the listed canonical type names in `type`.
- Include each allocated type at most once. You may omit types with zero allocation.
- Each count must be a non-negative integer.
- The sum of all counts must be at least 1 and no greater than the target question count.
- Use the weights to guide the overall mix where the chapter supports it; they do not require a fixed ratio.
- Return no Markdown fences, commentary, or additional JSON properties.
