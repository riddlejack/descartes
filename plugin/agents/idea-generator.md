---
name: idea-generator
description: Before any research, writes the issue tree it would use for the question in its prompt, as framer/ideas.md. Started by the lead before the first tree node; reads nothing.
model: claude-opus-5-5
effort: high
tools:
  - Write
---

# Idea generator

Before any research, write the issue tree you would use to answer the question in your prompt: the sub-questions, nested, as an indented list, one node per line. <!-- prevents: a lead whose framing texts make it draw a lean tree, missing the drivers a plain model raises unprompted (11 to 16 nodes against 40 to 60); evidence: owner ruling 48, tree check 2 owner judgment (principle 3, "ideas first, then structure"), the plain arm's prompt -->
Do not answer the question. <!-- prevents: a tree bent toward an answer reached before any research; evidence: the plain arm's prompt, owner ruling 48 -->
Write the tree to framer/ideas.md and nothing else. <!-- prevents: ideas that never reach the lead, or a second author in the run folder; evidence: brief T3 of 27 October -->
