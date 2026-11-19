---
name: Task compliance
description: Did implementation deliver the approved outcome and only that?
agents: [implement]
labels:
  - value: compliant
    score: 1
  - value: partial_or_expanded
    score: 0
passingScore: 1
samplingRate: 25
model: auto
selfImprovement: true
---
Compare the exact candidate with the approved outcome. Classify it as compliant
only when all requested behavior is present and no unapproved scope was added.
