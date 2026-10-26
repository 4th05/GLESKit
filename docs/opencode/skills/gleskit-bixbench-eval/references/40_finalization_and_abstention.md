# Finalization And Abstention

Final answer rules:

- return exactly one JSON object
- do not add prose before or after the JSON
- prefer a best-effort tool-grounded answer over silence
- abstain only when the available tool outputs still do not support the requested answer
- use this hidden support ladder before you finalize:
  - `accept`: the answer is directly and clearly supported
  - `mostly_supported`: the answer is strongly supported with only modest ambiguity
  - `partial`: some support exists, but the answer must keep a visible caveat
  - `weak`: the answer would overstate support; abstain
  - `unsupported`: the answer is not supported; abstain
- do not print the category token in the visible answer
- an enrichment `focus_status="not_found"` from the requested database, library, and background is an answer-bearing result: state that the target was not detected and finalize rather than varying the analysis parameters

Before finalizing, check:

1. Did a tool already return the requested answer directly?
2. Did I avoid unnecessary reads after that answer-bearing output?
3. Am I citing short evidence items such as file paths, contrasts, top terms, or compact tool results?
4. If support is only partial, is the caveat visible in the answer text?
5. Am I returning only JSON?

Abstain only when the answer is genuinely unsupported after real tool use.
