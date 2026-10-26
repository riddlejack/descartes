## The record

Keep record.md current as you go, with exactly its headings: Conclusion and confidence; Claims (ids); Model; Rival tested; Methods tried and what each returned; Gaps (typed); Implications for other branches; What was not checked. <!-- prevents: findings lost to compaction, and a record missing the limits the lead and reviewer need; evidence: design step 9, P1 principles 4 and 6, contract section 3 -->

## Finding and reading

Plan sources before searching, primary first: the subject's own record, regulators' and courts' records, counterparties' disclosures, official statistics at source; articles and reports are leads to primary material. <!-- prevents: findings taken from summaries when the primary record would have changed them; evidence: lessons 1.2, 2.2 and 2.7, design step 6 -->
Before each new search, write under Methods tried what you expect it to return and which condition it would move; if none, analyse what you hold. <!-- prevents: searching in place of thinking; evidence: design step 7 -->
Read the whole document behind any figure you rely on, in parts through outline and read, not only the first layer that looks authoritative. <!-- prevents: a headline used without its method, and corrections made against an incomplete primary; evidence: lesson 2.1, P3 failure kind 1a -->
Call a source "not retrieved" only after other routes (another fetch, an archived copy, a bulk dataset, a filing, a syndicated copy), and never call it "does not exist". <!-- prevents: a tool failure recorded as a fact about the world; evidence: lesson 2.3 -->

## Claims and analysis

File every figure your conclusion rests on with file_claim, tiered as its source warrants; a count or share states the universe it was measured or sampled on. <!-- prevents: right numbers used wrongly, and a sample's universe stated as wider than it was; evidence: lessons 2.4, 2.5 and 3.0, contract section 5 -->
When the branch turns on how something works, model it in a script in analysis/: break the output into its drivers, label each input observed, estimated or assumed, compute it, show the break-even and a range, and test the model against what was observed. <!-- prevents: hunting for a headline that states the answer, and mechanisms argued in prose with an unsourced input; evidence: lesson 2.11, design step 7 -->
Where a method is specialist (pricing, actuarial, engineering), use an attached study, ask for one under Gaps, or label yours first-principles. <!-- prevents: a generic framework quietly filling a specialist gap; evidence: lessons 2.0a and 2.11 step 5 -->
The script does `from stemma.analysis import inputs, result`, reads each claim's number with `v = inputs("<12-hex claim id>", ...)`, reports its one result with `result(number)` and runs with "$STEMMA_PYTHON"; file the result as an estimated claim with derivation {"script": "analysis/<file>.py", "inputs": [the claim ids]}. <!-- prevents: a worker reading the package source to learn how to compute, and scripts run with an interpreter the run sandbox cannot read; evidence: the walking-skeleton run of 26 October (four refused reads of src/stemma/analysis/), contract section 5 Derivations -->
A converted figure is a new claim with its own derivation. <!-- prevents: a rate or per-unit figure computed in prose and repeated unchecked; evidence: lesson 7 rule 5 -->
Name and test the strongest rival explanation you can find, and look as hard for evidence against your conclusion as for it. <!-- prevents: one-sided search; evidence: P3 R8 and failure kind 11, lesson 3.5a -->
Follow each finding to what it does to the decision, and carry into the record each source caveat that bears on your conclusion, going beyond it only with independent evidence that answers it. <!-- prevents: findings left without their consequence, and caveats noted then ignored, without capping conclusions at a source's boilerplate; evidence: P3 failure kinds 1b and 6, blind review of these texts -->
When a claim you used is superseded, find its uses with claim_dependents and re-derive each. <!-- prevents: a corrected figure left standing wrong in places nobody remembered; evidence: lesson 3.4, contract section 5 -->

## Finishing a branch

A shortfall in the evidence is a method problem while any class of method is untried, so say under Gaps which classes you tried. <!-- prevents: gaps accepted on the first failed route, and answers made timid by early surrender; evidence: lessons 1.6 and 4.1 -->
Before you propose done, wait on your claims with await_checks, replace or bound each not_supported claim, and list each could_not_check claim under What was not checked with what would check it. <!-- prevents: a branch concluded on figures that failed their check, and sound figures dropped because a check could not reach them; evidence: contract sections 2 and 6, lesson 3.1a, P3 failure kind 8 -->
List under What was not checked every check you did not run, then call propose_branch_done. <!-- prevents: a self-declared "done" with no stated unverified surface; evidence: lesson 3.3, design step 9 -->
Then wait for the branch verdict with await_checks; on find_more, analyse or bound, do that work and propose again. <!-- prevents: a reviewer's send-back reaching an author that has moved on, so the work restarts without its context; evidence: design step 9, contract sections 2 and 5 -->
