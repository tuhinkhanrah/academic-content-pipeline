# Generation Rules
- Follow runtime constraints from user prompt (question count, grade, penalty, tags).
- Treat runtime question count as a maximum cap, not a mandatory fixed count.
- Choose the actual number dynamically from 0..max based on concept quality, novelty, and solvability in the current page/content block.
- Prefer fewer high-quality questions over filler; output 0 questions when the source block is weak or non-assessable.
- Prefer conceptual variety across generated questions:
- standard MCQ
- assertion-reason
- statement-based
- match/list style
- Include at least one multi-concept question when the source supports it.
- Every question must be standalone and solvable without external context.

# Exam Calibration
- NEET: direct, speed-oriented, light calculation, high conceptual clarity.
