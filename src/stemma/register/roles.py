"""Role names as the harness reports them (`agent_type`), and who may do what.

These are logical invariants of the design (CONTRACTS sections 2 and 6), not tuning.
"""

from typing import Final

PLUGIN: Final = "stemma"

LEAD: Final = "stemma:lead"
WORKER: Final = "stemma:worker"
BLIND_FRAMER: Final = "stemma:blind-framer"
CLAIM_CHECKER: Final = "stemma:claim-checker"
BRANCH_REVIEWER: Final = "stemma:branch-reviewer"
DECIDERS_EYE: Final = "stemma:deciders-eye-reviewer"
EVIDENCE_AUDITOR: Final = "stemma:evidence-auditor"
MECHANICAL_READER: Final = "stemma:mechanical-reader"
RETRO_WRITER: Final = "stemma:retrospective-writer"

# Authors start authors; the host starts every check (section 2). The only subagent
# type any session may start is the worker (who may be a branch lead).
SUBAGENT_TYPES: Final = frozenset({WORKER})

# Sessions the driver starts, never a subagent.
CHECKING_ROLES: Final = frozenset(
    {
        BLIND_FRAMER,
        CLAIM_CHECKER,
        BRANCH_REVIEWER,
        DECIDERS_EYE,
        EVIDENCE_AUDITOR,
        MECHANICAL_READER,
        RETRO_WRITER,
    }
)

ALL_ROLES: Final = CHECKING_ROLES | {LEAD, WORKER}

# Authors: the roles that file claims and change the tree.
AUTHORS: Final = frozenset({LEAD, WORKER})

FINAL_REVIEWERS: Final = (DECIDERS_EYE, EVIDENCE_AUDITOR, MECHANICAL_READER)

MCP_PREFIX: Final = "mcp__plugin_stemma_stemma__"


def short(role: str) -> str:
    """`stemma:claim-checker` -> `claim-checker`."""
    return role.split(":", 1)[1] if role.startswith(PLUGIN + ":") else role
