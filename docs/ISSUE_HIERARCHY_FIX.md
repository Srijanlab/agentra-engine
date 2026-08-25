# Fix: Use GitHub Native Sub-Issues Instead of Confusing "Part 1/2" Titles

## Problem

Currently, multi-part features like #54 (deterministic infra-cost gate) create multiple issues with confusing patterns:

- **#54**: Parent issue (vague title, spec in comments)
- **#55**: Tracking issue (title: "Part 1/2...")
- **#56**: Sub-issue of #55 (SAME title as #55, closed)
- **#57**: Sub-issue of #55 (title: "Part 2/2...", closed)
- **#58**: Related issue (title: "Part 2/2...", BUT different from #57)
- **#62, #63**: Follow-up issues

**Why it's confusing:**
- ❌ #55 and #56 have identical titles
- ❌ Multiple "Part 2/2" issues (#57 vs #58)
- ❌ No formal parent-child relationships in GitHub
- ❌ Issues marked `status:shipped` but left OPEN (confusing status)
- ❌ Relationships only visible in titles/comments, not in GitHub UI

---

## Solution: Use GitHub Native Sub-Issues

GitHub's native sub-issues feature (API since 2023) supports:
- Up to 100 sub-issues per parent
- Up to 8 levels of nesting
- Auto-calculated progress (X/Y sub-issues complete)
- Native GitHub UI visualization
- REST API support

### How It Should Work

**Issue #54 (Parent - Epic)**
```
Title: Deterministic infra-cost gate: add schema + Python enforcement
State: open
Labels: feature, in-progress, agentra
Sub-issues: 5/5 complete ← GitHub auto-calculates this
├── #56 Add infra_cost_impact field to architecture_review.py schema
├── #57 Add category parameter to _escalate_to_human helper
├── #58 Implement deterministic gate in implement_feature()
├── #62 Update Architecture Review Agent prompt
└── #63 Thread category through session.mark_waiting_for_human
```

**Each sub-issue (e.g., #56)**
```
Title: Add infra_cost_impact field to architecture_review.py schema
State: open → closed (when merged/verified)
Labels: story, agentra
Parent: #54 ← Formal link back to parent
```

---

## Required Changes to Code

### 1. **agentra/memory/core.py** - Add Sub-Issue Support Labels

```python
# Add after line 27:
_PARENT_ISSUE_LABEL = "parent-issue:"  # Used when REST API doesn't support parent_issue param
```

### 2. **agentra/memory/github_issues.py** - Add Sub-Issue Creation Method

Create new method in Memory class:

```python
def create_sub_issue(
    self,
    parent_issue_number: int,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
) -> dict | None:
    """
    Create a sub-issue under a parent issue using GitHub REST API.
    
    Args:
        parent_issue_number: The parent issue number
        title: Sub-issue title (should be descriptive, NOT "Part X/Y")
        body: Sub-issue description
        labels: Optional labels to add (e.g., ["story", "agentra"])
    
    Returns:
        Created issue dict with 'number' and 'html_url' keys, or None on failure
    
    Example:
        sub_issue = mem.create_sub_issue(
            parent_issue_number=54,
            title="Add infra_cost_impact field to architecture_review.py schema",
            body="Changes:\n- Extend output schema\n- Add infra_cost_impact field",
            labels=["story", "agentra"]
        )
    """
    if labels is None:
        labels = []
    
    # GitHub REST API endpoint for creating sub-issues
    # POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues
    payload = {
        "title": title,
        "body": body,
        "labels": labels,
    }
    
    try:
        response = self.gh.create_issue_sub_issue(
            self.owner,
            self.repo,
            parent_issue_number,
            payload,
        )
        return response
    except Exception as e:
        logger.error(f"Failed to create sub-issue of #{parent_issue_number}: {e}")
        return None


def add_issue_as_sub_issue(
    self,
    parent_issue_number: int,
    sub_issue_number: int,
) -> bool:
    """
    Link an existing issue as a sub-issue of a parent.
    
    Args:
        parent_issue_number: The parent issue number
        sub_issue_number: The sub-issue number
    
    Returns:
        True if successful, False otherwise
    
    Example:
        mem.add_issue_as_sub_issue(54, 56)
    """
    try:
        self.gh.add_issue_sub_issue(
            self.owner,
            self.repo,
            parent_issue_number,
            sub_issue_number,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to link #{sub_issue_number} as sub-issue of #{parent_issue_number}: {e}")
        return False
```

### 3. **agentra/agents/brain/tools.py** - Update Issue Filing Logic

In `_escalate_to_human()` function (line 105-143), modify to support parent issues:

```python
def _escalate_to_human(
    session: OrchestratorSession,
    *,
    diagnosis: str,
    question: str,
    source: str,
    title: str,
    branch: str | None = None,
    tracking_issue: int | None = None,
    parent_issue: int | None = None,  # NEW: parent issue for sub-issue link
    category: str | None = None,  # NEW: category for this escalation (e.g., "infra_cost")
) -> int | None:
    """Human-in-the-loop escalation (GitHub issue #34), shared by implement_feature 
    and discover_opportunities' HUMAN_INPUT_REQUIRED branches.
    
    Now supports creating issues as sub-issues of a parent epic/feature.
    """
    issue_number = session.mem.record_known_bug(
        session.run_id, "medium", diagnosis,
        "Requires an explicit human decision -- not an implementation/discovery failure, "
        "not something a different brief/approach fixes.",
        source=source,
        needs_human=True,
        title=title,
    )
    
    # If this should be a sub-issue of a parent, link it
    if issue_number is not None and parent_issue is not None:
        session.mem.add_issue_as_sub_issue(parent_issue, issue_number)
    
    # Add category to issue body if provided (for tracking infra_cost vs product_direction, etc.)
    if issue_number is not None and category is not None:
        session.mem.add_comment_to_issue(
            issue_number,
            f"Category: {category}"
        )
    
    issue_url = session.mem.issue_html_url(issue_number) if issue_number is not None else None
    if issue_number is not None:
        session.mem.record_human_input_context(
            issue_number, app=session.app_name, run_id=session.run_id, question=question,
            branch=branch, session_id=session.session_id, tracking_issue=tracking_issue,
        )
    from agentra import urls
    from agentra.connectors import slack

    slack.notify_human_input_required(
        app=session.app_name,
        run_id=session.run_id,
        question=question,
        issue_url=issue_url,
        dashboard_url=urls.dashboard_run_url(session.run_id, session.app_name),
        branch=branch,
        session_id=session.session_id,
    )
    session.mark_waiting_for_human(issue_number=issue_number, issue_url=issue_url, question=question, branch=branch)
    return issue_number
```

### 4. **Update `implement_feature` Tool** (line 378-556)

When filing sub-issues for multi-part features, use `parent_issue` parameter:

```python
# Around line 481, when calling _escalate_to_human for implement_feature:
_escalate_to_human(
    session,
    diagnosis=diagnosis,
    question=question or reason,
    source="implementation-agent-human-input-required",
    title=f"Human input required: {feature_name}",
    branch=session.feature_branch,
    tracking_issue=tracking_issue,
    parent_issue=None,  # Don't make human-escalation issues into sub-issues
    category="implementation",  # Categorize this type of escalation
)
```

---

## Migration Strategy

### Phase 1: Add Sub-Issue Support (No Breaking Changes)
1. Add `create_sub_issue()` and `add_issue_as_sub_issue()` methods to Memory
2. Add `parent_issue` and `category` parameters to `_escalate_to_human()`
3. Keep old behavior: issues still created without explicit parent links

### Phase 2: Use Sub-Issues for New Multi-Part Features
1. When agent detects a multi-part feature (e.g., new epic #54), create it as parent
2. Auto-create sub-issues with descriptive titles (NO "Part X/Y")
3. Link them using `add_issue_as_sub_issue()` or REST API `parent_issue` param

### Phase 3: Retroactive Migration (Optional)
1. Manually link existing issues (#56, #57, #58 → parent #54) OR
2. Create a bot task to find all "Part X/Y" titled issues and auto-link them

---

## New Workflow for Multi-Part Features

**When a multi-part feature is detected:**

```python
# Step 1: Create parent epic
parent = session.mem.record_feature_request(
    body="Deterministic infra-cost gate implementation plan...",
    source="github",
    title="Deterministic infra-cost gate: add schema + Python enforcement",
    extra_labels=["feature", "in-progress", "agentra"]
)
parent_issue_num = parent["number"]

# Step 2: Create sub-issues with DESCRIPTIVE titles
sub1 = session.mem.create_sub_issue(
    parent_issue_num,
    title="Add infra_cost_impact field to architecture_review.py schema",
    body="...",
    labels=["story", "agentra"]
)

sub2 = session.mem.create_sub_issue(
    parent_issue_num,
    title="Add category parameter to _escalate_to_human helper",
    body="...",
    labels=["story", "agentra"]
)

# Step 3: When work ships, just close the sub-issue
# GitHub automatically updates parent progress: "2/X sub-issues complete"
```

---

## Benefits

| Aspect | Before | After |
|--------|--------|-------|
| **Title confusion** | "Part 1/2", "Part 2/2" duplicates | Descriptive, unique titles |
| **Hierarchy** | Implicit (titles only) | Explicit (GitHub UI shows tree) |
| **Progress tracking** | Manual | Auto-calculated (X/Y complete) |
| **Finding related issues** | Search by title/comments | Query `parent: #54` in GitHub |
| **Status clarity** | `open + status:shipped` confusion | Close when done, parent stays open |
| **Projects integration** | No parent-child filtering | Native parent filter in projects |

---

## Testing Checklist

- [ ] `create_sub_issue()` creates issue with parent_issue relationship
- [ ] `add_issue_as_sub_issue()` links existing issue as sub-issue
- [ ] GitHub UI shows parent-child relationship (click parent, see sub-issues listed)
- [ ] Progress tracker shows X/Y sub-issues complete
- [ ] Close sub-issue → parent progress updates automatically
- [ ] REST API query `/repos/owner/repo/issues/54` returns sub-issues array
- [ ] Old multi-part features (#54-63) can be retroactively linked

---

## References

- [GitHub Docs: Adding sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues)
- [GitHub REST API: Create sub-issue](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#create-a-sub-issue)
- Current issues structure: #54 (parent), #56-57 (sub-issues of #55), #58-63 (related but unlinked)
