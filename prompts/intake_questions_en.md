# Intake Questions — English

Questions organized by case type. Ask one at a time. Adapt follow-up based on answer.

---

## Universal Questions (all case types)
Ask these before case-type specific questions.

| Priority | Question | Field |
|---|---|---|
| 1 | "How long have you been living in the United States?" | `years_in_us` |
| 2 | "How did you enter the US? For example, with a visa, at the border, or another way?" | `entry_method` |
| 3 | "Do you have any immediate family members who are US citizens or green card holders?" | `us_family_connections` |

---

## Family-Based Petitions

| Priority | Question | Field | Notes |
|---|---|---|---|
| 1 | "Who are you hoping to sponsor, or who is sponsoring you?" | `petitioner_relationship` | Spouse, parent, child, sibling |
| 2 | "Is the US citizen or permanent resident spouse/family member a US citizen or do they have a green card?" | `petitioner_status` | |
| 3 | "What is your current marital status?" | `marital_status` | |
| 4 | "Do you have any children who would also be immigrating?" | `num_dependents` | |
| 5 | "Are any of your children currently in the US?" | `dependents_in_us` | |

---

## Employment-Based Visas

| Priority | Question | Field | Notes |
|---|---|---|---|
| 1 | "Do you currently have an employer who would sponsor your visa?" | `employer_willing_to_sponsor` | |
| 2 | "What kind of work do you do, and what is your job title?" | `job_title` | |
| 3 | "What is the highest level of education you've completed?" | `education_level` | |
| 4 | "How many years of experience do you have in your field?" | `years_experience` | |
| 5 | "What is the name of the company or employer interested in sponsoring you?" | `employer_name` | Skip if no employer yet |

---

## Asylum

| Priority | Question | Field | Notes |
|---|---|---|---|
| 1 | "When did you arrive in the United States?" | `arrival_date_us` | Used to check 1-year deadline |
| 2 | "Have you already filed for asylum, or is this your first inquiry?" | `has_filed_asylum` | |
| 3 | "What country are you from?" | `country_of_persecution` | |
| 4 | "Can you tell me generally why you left — for example, was it related to your religion, political views, or your identity?" | `persecution_type` | General only — do NOT ask for detailed narrative |

> Note: Never ask for detailed abuse or trauma narratives. Collect general category only (religion, nationality, political opinion, social group, race). Flag for attorney.

---

## Removal / Deportation Defense

| Priority | Question | Field | Notes |
|---|---|---|---|
| 1 | "Have you received an official document called a Notice to Appear, or NTA?" | `has_nta` | |
| 2 | "Do you have an immigration court hearing date scheduled?" | `has_court_date`, `court_date` | |
| 3 | "Where is your immigration court located?" | `court_location` | |
| 4 | "Have you ever been deported or removed from the US before?" | `prior_deportation` | |
| 5 | "Are you currently being held in an immigration detention center?" | `is_detained` | Already asked in urgency triage |

---

## DACA

| Priority | Question | Field | Notes |
|---|---|---|---|
| 1 | "When does your current DACA expire?" | `visa_expiration_date` | |
| 2 | "Have you had any changes in your situation since your last renewal — new address, travel outside the US, any legal issues?" | Track in `extra_data.daca_notes` | |
| 3 | "What year did you first arrive in the US?" | Infer `years_in_us` | |

---

## Naturalization / Citizenship

| Priority | Question | Field | Notes |
|---|---|---|---|
| 1 | "How long have you been a permanent resident (green card holder)?" | Infer from `years_in_us` | |
| 2 | "Have you traveled outside the US for more than 6 months at a time in the last 5 years?" | `extra_data.long_trips_abroad` | |
| 3 | "Have you had any criminal issues or arrests in the US or abroad?" | `has_criminal_record` | Yes/no only |

---

## Document Checklist (all case types, Tier 3)
Ask only if caller has time after case-specific questions.

> "Just a quick checklist — do you have any of the following documents available? Yes or no is fine."

| Document | `document_type` |
|---|---|
| Current passport | `passport` |
| Birth certificate | `birth_certificate` |
| Any US visa stamps or entries | `visa_stamps` |
| I-94 travel record | `i94` |
| Evidence of US ties (lease, tax returns, employment letter) | `employment_letter`, `tax_returns` |
| Marriage certificate (if applicable) | `marriage_certificate` |

---

## Handling Sensitive Topics

**Criminal record:**
- Ask yes/no only: "Have you ever been arrested for or convicted of any crime in the US or in another country?"
- Do NOT ask for details, charges, dates, or outcomes — that is for the attorney
- Store `has_criminal_record = TRUE/FALSE` only

**Prior visa denials:**
- Ask yes/no only: "Have you ever been denied a visa or had an immigration application rejected?"
- Do NOT ask for details — attorney will review

**Prior immigration fraud:**
- If caller volunteers this information, say: "I appreciate your honesty. Please share those details with the attorney — they're in the best position to advise you confidentially."
- Do NOT record the details in intake

---

## Graceful Skip Phrases

Use these when the caller doesn't know or doesn't want to answer:

- "No problem, the attorney will go over that in your consultation."
- "That's okay — let me make a note that we'll need to discuss that."
- "You don't need to have that information right now."
