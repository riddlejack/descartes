"""Reading a finished (or stopped) run folder for people: the static report viewer
(`stemma report`), the offline re-check (`stemma verify`) and the tamper demo.

Everything here reads a run folder and never writes into it. The viewer and verify
judge nothing: they show what the run's agents recorded and re-test exact facts
(bytes, quotations, recomputation, fingerprints).
"""
