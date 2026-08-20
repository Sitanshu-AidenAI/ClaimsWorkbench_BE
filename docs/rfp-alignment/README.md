# Requirements alignment pack

An assessment of Claims Workbench against its three source documents, consolidated into one
requirement matrix and one build plan.

## The three sources

| # | Document | What it contributes |
| --- | --- | --- |
| 1 | `Claims_RP_RFP_Scope_Description_Final.docx` | The Allianz AGCS request for proposal. Section 1 e-FNOL, Section 2 light-touch & augmented processing, thirteen acceptance criteria applied to both. **125 requirements.** |
| 2 | `01042025_Claim Segmentations_LTP.pdf` | Attachment 2 to the RFP. One slide, and it turns Section 2 from vague into buildable: six claim segments, an admin lane automated for all of them, four named loss-assessment lanes. **13 requirements.** |
| 3 | `Claims Workbench_BRD.docx` | AidenAI's own Business Requirements Document. Adds four capability areas the RFP never mentions, and draws an MVP boundary the RFP does not accept. **50 requirements.** |

Assessed at `feature/policy_identification` / `2a56bf7`, across `ClaimsWorkbench_BE`
(~50,500 lines Python, 74 endpoints, 30 tables, 832 tests) and `ClaimsWorkbench_FE`
(~63,300 lines TypeScript, 17 feature areas). Statuses come from reading the code.

## The four documents in this pack

| File | What it is | Read it if |
| --- | --- | --- |
| `01_Requirements_Explained` (`.md` / `.docx` / `.pdf`) | All three sources explained from scratch for a non-insurance reader. Decoder ring, both current-state processes, the segmentation grid, the thirteen acceptance criteria as plain questions, what the BRD adds, and where the three documents disagree. | You need to understand what is being asked for. |
| `02_Requirements_Matrix.xlsx` | **195 requirements** mapped to modules, with status, coverage, the code path that proves it, the gap, effort, priority, and whether it sits inside the BRD's MVP. Eight sheets, including the segmentation model and a sequenced build plan. | You need line-item detail, or to price the work. |
| `03_Misalignments` (`.md` / `.docx` / `.pdf`) | The **twenty** places where the product and the documents actively conflict, as opposed to the 131 places where work is simply outstanding. Four groups; Group D is what the two new documents surfaced. | You need to know what would sink a bid. |
| `04_Build_Plan` (`.md` / `.docx` / `.pdf`) | **What is to be built**, in order. Six phases plus a parallel multilingual track, gated on one decision and four requests. | You need to know what to do on Monday. |

## Headline

- **62 of 193** technical requirements Built, **43** Partial, **3** Prototype (UI only), **84** Not built, **1** Misaligned. Weighted coverage **44%**.
- **The number depends on which document is the plan.** Inside the BRD's declared MVP: 124 requirements at **58%** coverage. Outside it — almost exactly RFP Section 2 and Attachment 2: 33 requirements at **11%**. Real RFP requirements the BRD does not cover at all: 35 at **22%**.
- The nine CLAWS entry categories average **41%** — strong on the first three and on review, near-zero on coverages, party-to-coverage links, deductibles and movement types.
- The reading half is genuinely built, and two parts of it beat what any of the documents asks for. The writing half — CLAWS, FileNet, Global Genius, SharePoint, CARA — does not exist.

## Before anything else

**One decision.** The BRD's §4 excludes from MVP most of what RFP Section 2 and Attachment 2
require. 33 of 195 requirements sit outside it. Until somebody writes down which reading wins,
roughly six months of the build plan is speculative. See misalignment M16.

**Four requests to Allianz.** None is a build; all four gate work in the plan.

1. `01042025_BMP_CLAWS_EFNOL.xlsx` (Attachment 1) — the ~180 CLAWS attributes that
   criterion 1's 95% accuracy is measured against. Gates three of the largest work packages.
2. Allianz internal IT security standards and the approved-technology list.
3. Which uptime figure binds — 96% per year, or 99.9% for the API. A 40× difference in error budget.
4. Where attachment passwords come from. Gates the password-protected-content work entirely.

Plus the AQS ruleset, and Attachment 3 (the rollout plan) to hold durations against real dates.

## Regenerating

The Markdown files are the source for documents 1, 3 and 4. The workbook's 195 requirement rows
live in one Python list, so a status change is a one-line edit; the generator scripts are
referenced in this pack's commit.
