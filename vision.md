Here’s a clean **product + engineering spec document** you can use as your master reference.

You can copy this directly into a `.md` file (I’ve formatted it as production-ready documentation).

---

```markdown
# Autonomous Product Engineering Agent System

## 1. Vision

Build a **local AI-powered autonomous engineering system** that continuously improves a web application by:

- Understanding the codebase
- Analyzing user behavior and analytics
- Discovering missing features automatically
- Implementing features end-to-end
- Testing changes safely
- Deploying to beta environments
- Learning from real-world results

The system behaves like a **self-improving software engineering team**, not a coding assistant.

---

## 2. Key Principle

This system does NOT require feature-level instructions from humans.

Instead:

### Traditional workflow
Human:
> "Add friend challenges"

### This system
Human:
> "Improve engagement and retention"

Agent:
- Decides what features to build
- Implements them
- Tests them
- Deploys them
- Measures impact

---

## 3. Scope

This system is **general-purpose**, not tied to any single project.

It can operate on:

- Sports prediction apps (initial use case)
- SaaS products
- E-commerce platforms
- Internal dashboards
- APIs and backend systems

Only these inputs change per project:
- Business objective
- Analytics data
- Domain context

---

## 4. System Architecture

```

```
            Human Objective
                    ↓
         Orchestrator Agent (Brain)
                    ↓
```

┌──────────────────────────────────────────────┐
│                                              │
│   Codebase Agent        Product Agent        │
│   (understanding)       (feature discovery)  │
│                                              │
└──────────────────────────────────────────────┘
↓
Research + Planning Agent
↓
Implementation Agent
↓
Testing Agent (QA)
↓
Deployment Agent (Beta)
↓
Analytics Feedback Loop
↓
Continuous Loop

````

---

## 5. Core Agents

### 5.1 Orchestrator Agent

Responsibilities:
- Receives high-level business goals
- Coordinates all sub-agents
- Maintains execution state
- Decides next best action

---

### 5.2 Codebase Understanding Agent

Responsibilities:
- Scan repository structure
- Identify frameworks and architecture
- Map routes, APIs, and services
- Understand data models and flows

Output example:
```json
{
  "framework": "Next.js",
  "backend": "Firebase",
  "features": ["brackets", "predictions"],
  "architecture": "serverless"
}
````

---

### 5.3 Product Discovery Agent (CRITICAL)

This is the most important component.

It identifies **what to build next without being told**.

Inputs:

* Analytics (Firebase / PostHog / Mixpanel)
* User behavior funnels
* App codebase structure
* Competitor analysis

Responsibilities:

* Detect drop-offs in user flow
* Identify missing engagement loops
* Find unused or underused features
* Analyze competitor features
* Generate feature opportunities

Output:

```json
[
  {
    "feature": "prediction_streaks",
    "impact": "high",
    "effort": "low",
    "reason": "Users return inconsistently after match day"
  },
  {
    "feature": "friend_challenges",
    "impact": "very_high",
    "effort": "medium",
    "reason": "No social competition mechanism exists"
  }
]
```

---

### 5.4 Research Agent

Responsibilities:

* Find implementation approaches
* Compare libraries and frameworks
* Study competitor implementations
* Evaluate tradeoffs

Output:

```json
{
  "recommended_solution": "...",
  "alternatives": ["..."],
  "risks": ["..."]
}
```

---

### 5.5 Planning / Architect Agent

Responsibilities:

* Convert feature into technical design
* Define system changes
* Identify affected modules

Output:

```json
{
  "frontend_changes": [],
  "backend_changes": [],
  "database_changes": [],
  "api_changes": [],
  "risk_level": "low"
}
```

---

### 5.6 Implementation Agent

Responsibilities:

* Modify source code
* Add new features
* Refactor when necessary
* Ensure build success

Execution loop:

```
Implement → Test → Fix → Retry
```

Tools:

* File system access
* Git operations
* Shell commands (restricted)

---

### 5.7 Testing Agent

Responsibilities:

* Run automated test suite
* Perform browser testing (Playwright)
* Validate feature correctness

Runs:

* Lint
* Typecheck
* Unit tests
* Integration tests
* E2E tests

Output:

```json
{
  "status": "pass",
  "failed_tests": []
}
```

---

### 5.8 Deployment Agent (Beta Only)

Responsibilities:

* Deploy to preview/staging environments
* Create pull requests
* Run smoke tests post-deployment

Strict restriction:

* ❌ No production deployment without human approval

---

### 5.9 Analytics Feedback Agent

Responsibilities:

* Measure feature impact
* Track engagement changes
* Monitor retention and conversion
* Feed results back into system

Metrics:

* Daily Active Users (DAU)
* Retention rate
* Sharing rate
* Conversion funnels

---

## 6. Autonomous Execution Loop

```
1. Load codebase context
2. Read analytics
3. Identify problems/opportunities
4. Generate feature ideas
5. Rank by impact vs effort
6. Select top feature
7. Design solution
8. Implement feature
9. Run tests
10. Deploy to beta
11. Measure impact
12. Repeat cycle
```

---

## 7. Tooling Requirements

Local system must provide:

### Code & File Tools

* Read/write files
* Search codebase
* AST or grep-based analysis

### Dev Tools

* Git (branch, commit, PR)
* Shell execution (restricted allowlist)

### Testing Tools

* Playwright (browser automation)
* Unit test runner
* Build system (npm, etc.)

### Deployment Tools

* Vercel CLI or equivalent
* Firebase CLI or backend deploy system

### Analytics Tools

* Firebase Analytics / PostHog / Mixpanel

---

## 8. Safety Constraints

### Allowed Actions

* Modify code
* Create branches
* Run tests
* Deploy to staging/beta
* Generate PRs

### Forbidden Actions

* Production deployment (without approval)
* Deleting production data
* Modifying secrets or billing
* Irreversible destructive operations

---

## 9. Memory System

Persistent knowledge storage:

```
/docs/architecture/
/docs/decisions/
/docs/features/
/docs/metrics/
/docs/failures/
```

Stores:

* Architectural decisions
* Coding conventions
* Past feature performance
* Known issues

---

## 10. MVP Plan

### Phase 1 (Minimum Viable System)

* Single agent
* Code understanding
* Feature implementation
* Testing loop
* Preview deployment

### Phase 2

* Add Product Discovery Agent
* Analytics-driven decision making
* Feature ranking system

### Phase 3

* Fully autonomous weekly improvement cycle
* Continuous beta deployments
* Self-improving product loop

---

## 11. Example Behavior

### Input:

```
Improve engagement for the sports app
```

### Agent output:

* Detects low sharing rate
* Identifies missing social loop
* Finds no retention mechanics

### Actions:

* Implements prediction streaks
* Adds friend challenges
* Adds badges system
* Deploys to beta

### Result:

* Reports engagement increase

---

## 12. Final Vision

This system becomes:

> A self-improving AI engineering team that continuously evolves a product without needing feature-by-feature instructions.

Human role shifts from:

* Writing features

to:

* Defining goals and constraints

```

---

If you want next step, I can help you turn this into:

- :contentReference[oaicite:0]{index=0}
- or :contentReference[oaicite:1]{index=1}
- or :contentReference[oaicite:2]{index=2}
```
