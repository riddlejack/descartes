---
name: card-mechanism
description: Question-type card for a question about how something works or what a change would do, such as the effect of a price change. Load it when the answer turns on how something works.
---

# Card: a mechanism (what a change would do)

Use this card when the answer turns on how something works or what a change would do; you build the analysis rather than look for a source that states the answer.

## Building the model

Write the output as an equation of its drivers before collecting any number.
Label each input observed, estimated or assumed, with its source and the basis it is on (the price actually paid or the list price, one period or another).
Compute it in a script in analysis/, and show the break-even and the range over the inputs that move it most.
For example, a price cut pays only if volume rises enough to replace the margin it gives up, and the script shows how much that is.
Test the model against what was observed (past changes, reported results, peers), and investigate a large gap rather than averaging it away.

## Its limits

Say how competitors or customers could react, and whether the result holds if they do.
Say when the method is specialist (pricing studies, actuarial or engineering models), use an attached study or ask for one, and otherwise label the method first-principles.
