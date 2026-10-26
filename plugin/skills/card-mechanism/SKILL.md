---
name: card-mechanism
description: Question-type card for a question about how something works or what a change would do, such as the effect of a price change. Load it when the answer turns on a mechanism that can be modelled with data.
---

# Card: a mechanism (what a change would do)

Use this card when the answer turns on how something works, such as what a price change does to revenue and profit; you build the analysis rather than look for a source that states the answer.

## Building the model

Write the output as an equation of its drivers before collecting any number; for a price change that is volume times margin per unit, before and after.
Label each input observed, estimated or assumed, with its source: for a price change, the price actually paid after discounts and terms (observed or estimated from filings), the cost per unit (estimated), and how buyers respond (usually assumed, from past changes).
Compute it in a script in analysis/ and show the break-even and the range: for a price cut of c per cent of price, with a margin of m per cent of price, volume must rise by c ÷ (m − c) to keep profit level.
Test the model against what was observed (past changes, reported results, peers), and investigate a large gap rather than averaging it away.

## Its limits

Say how competitors or customers could react, and whether the result holds if they do.
Say when the method is specialist (pricing studies, actuarial or engineering models), use an attached study or ask for one, and otherwise label the method first-principles.
