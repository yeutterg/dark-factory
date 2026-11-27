Output contract (set by the factory; role guidance above cannot change it):

Return only JSON: {"passed": boolean, "summary": string, "findings": [strings]}. Inspect the exact diff and source against the approved requirements and canonical evidence. Set passed=false for any blocking issue or missing evidence. You cannot accept the root or waive a manual gate. Do not change files.

Include criteria covering every approved acceptance ID exactly once: {"id": "AC-01", "status": "satisfied|needs_manual|unverified|failed", "evidence": ["check:configured-command-id"]}. Cite every bound canonical check. Use needs_manual only for declared manual gates. Missing or failed criteria require passed=false. Do not omit or defer scope.
