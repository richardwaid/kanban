"""GitHub issue synchronization for the kanban agent system.

Polls GitHub for new issues with a trigger label and creates bug reports.
Posts triage results and PR links back to issues as comments.
"""

from __future__ import annotations

import asyncio
import logging
import os

import yaml

from controlplane import store
from controlplane.github_client import GitHubAppClient

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.environ.get("KANBAN_GITHUB_POLL_INTERVAL", "60"))
TRIGGER_LABEL = os.environ.get("KANBAN_GITHUB_TRIGGER_LABEL", "kanban-triage")
ACTIVE_LABEL = "kanban-active"
NEEDS_INFO_LABEL = "needs-info"

ISSUE_MAP_PATH = store.DATA_DIR / "github_issue_map.yaml"
ISSUE_COMMENT_CURSOR_PATH = store.DATA_DIR / "github_comment_cursors.yaml"


def load_comment_cursors() -> dict[int, int]:
    """Load the last-seen comment ID per issue number."""
    if not ISSUE_COMMENT_CURSOR_PATH.exists():
        return {}
    data = yaml.safe_load(ISSUE_COMMENT_CURSOR_PATH.read_text()) or {}
    return {int(k): int(v) for k, v in data.items()}


def save_comment_cursors(cursors: dict[int, int]) -> None:
    ISSUE_COMMENT_CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    ISSUE_COMMENT_CURSOR_PATH.write_text(yaml.dump(
        {int(k): int(v) for k, v in cursors.items()},
        default_flow_style=False,
    ))


def load_issue_map() -> dict[int, str]:
    """Load the GitHub issue number → feature ID mapping."""
    if not ISSUE_MAP_PATH.exists():
        return {}
    data = yaml.safe_load(ISSUE_MAP_PATH.read_text()) or {}
    return {int(k): v for k, v in data.items()}


def save_issue_map(mapping: dict[int, str]) -> None:
    """Save the GitHub issue number → feature ID mapping."""
    ISSUE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    ISSUE_MAP_PATH.write_text(yaml.dump(
        {int(k): v for k, v in mapping.items()},
        default_flow_style=False,
    ))


def import_issue(client: GitHubAppClient, issue: dict) -> str | None:
    """Import a single GitHub issue as a bug report. Returns the bug ID or None."""
    issue_num = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""

    feature, task = store.create_bug_report(title, body)

    # Store the mapping
    mapping = load_issue_map()
    mapping[issue_num] = feature.id
    save_issue_map(mapping)

    # Update feature with GitHub issue number
    feature.github_issue_number = issue_num
    store.save_feature(feature)

    # Update labels on GitHub
    try:
        client.update_labels(issue_num, add=[ACTIVE_LABEL], remove=[TRIGGER_LABEL])
        client.add_comment(issue_num,
            f"Bug report created as **{feature.id}**. Triage in progress.")
    except Exception:
        logger.exception("Failed to update GitHub issue %d", issue_num)

    logger.info("Imported GitHub issue #%d as %s: %s", issue_num, feature.id, title)
    return feature.id


def post_triage_result(client: GitHubAppClient, feature, triage_result: dict) -> None:
    """Post triage results back to the GitHub issue."""
    if not feature.github_issue_number:
        return

    issue_num = feature.github_issue_number
    verdict = triage_result.get("verdict", "")
    summary = triage_result.get("summary", "")
    questions = triage_result.get("questions", [])

    if verdict == "valid":
        body = f"**Triage: Valid bug** - Implementation in progress.\n\n{summary}"
        try:
            client.add_comment(issue_num, body)
        except Exception:
            logger.exception("Failed to post triage comment to issue #%d", issue_num)

    elif verdict == "needs_info":
        q_list = "\n".join(f"- {q}" for q in questions)
        body = (
            f"**Triage: More information needed**\n\n{summary}\n\n"
            f"**Please answer these questions:**\n{q_list}\n\n"
            f"_Update this issue's description with the requested info, "
            f"and the `{NEEDS_INFO_LABEL}` label will be removed automatically._"
        )
        try:
            client.add_comment(issue_num, body)
            client.update_labels(issue_num, add=[NEEDS_INFO_LABEL])
        except Exception:
            logger.exception("Failed to post triage comment to issue #%d", issue_num)


def post_review_to_pr(client: GitHubAppClient, feature, review_result: dict) -> None:
    """Post a code review result as a GitHub PR review."""
    if not feature.github_pr_number:
        return

    outcome = review_result.get("review_outcome", "")
    summary = review_result.get("summary", "")
    items = review_result.get("items", [])

    # GitHub Apps can't APPROVE or REQUEST_CHANGES on their own PRs,
    # so always use COMMENT. The emoji and formatting make the intent clear.
    event = "COMMENT"
    if outcome == "approved" and not any(i.get("type") == "bug" for i in items):
        verdict_prefix = "APPROVED"
    elif outcome == "needs_changes" or any(i.get("type") == "bug" for i in items):
        verdict_prefix = "CHANGES REQUESTED"
    else:
        verdict_prefix = "REVIEW"

    # Build review body
    body_parts = [f"**{verdict_prefix}**: {summary}"]
    if items:
        body_parts.append("")
        for item in items:
            item_type = item.get("type", "improvement").upper()
            priority = item.get("priority", "?")
            title = item.get("title", "")
            desc = item.get("description", "")
            emoji = "🐛" if item_type == "BUG" else "💡"
            body_parts.append(f"{emoji} **P{priority} {item_type}: {title}**")
            body_parts.append(f"  {desc}")
            body_parts.append("")

    body = "\n".join(body_parts)

    # Build line-level comments for items that have file + line info
    comments = []
    for item in items:
        f = item.get("file")
        line = item.get("line")
        if f and line:
            item_type = item.get("type", "improvement").upper()
            emoji = "🐛" if item_type == "BUG" else "💡"
            comments.append({
                "path": f,
                "line": int(line),
                "body": f"{emoji} **{item_type}: {item.get('title', '')}**\n\n{item.get('description', '')}",
            })

    try:
        if comments:
            # Post review with inline comments
            client._request(
                "POST", f"/repos/{client.repo}/pulls/{feature.github_pr_number}/reviews",
                json={"body": body, "event": event, "comments": comments},
            )
        else:
            client.create_pr_review(feature.github_pr_number, body, event)
        logger.info("Posted %s review to PR #%d for %s (%d inline comments)",
                     verdict_prefix, feature.github_pr_number, feature.id, len(comments))
    except Exception:
        # Fall back to review without inline comments
        logger.warning("Line-level review failed, posting summary only")
        try:
            client.create_pr_review(feature.github_pr_number, body, event)
        except Exception:
            logger.exception("Failed to post review to PR #%d", feature.github_pr_number)


def post_dispute_to_pr(client: GitHubAppClient, feature, item_responses: list[dict]) -> None:
    """Post worker's dispute responses as a PR comment."""
    if not feature.github_pr_number:
        return

    disputed = [r for r in item_responses if r.get("action") == "disputed"]
    if not disputed:
        return

    lines = ["**Worker pushed back on review items:**\n"]
    for r in disputed:
        lines.append(f"- **{r.get('title', 'Item')}**: {r.get('reason', 'No reason given')}")

    try:
        client.add_comment(feature.github_pr_number, "\n".join(lines))
        logger.info("Posted %d dispute(s) to PR #%d", len(disputed), feature.github_pr_number)
    except Exception:
        logger.exception("Failed to post disputes to PR #%d", feature.github_pr_number)


def post_escalation_to_pr(client: GitHubAppClient, feature,
                           escalated_items: list, worker_responses: list[dict]) -> None:
    """Post escalated disputes to the PR for human visibility."""
    if not feature.github_pr_number:
        return

    responses_by_title = {r.get("title", ""): r for r in worker_responses}

    lines = ["**Review disputes escalated to human:**\n"]
    for item in escalated_items:
        title = getattr(item, 'title', '') if hasattr(item, 'title') else item.get('title', '')
        desc = getattr(item, 'description', '') if hasattr(item, 'description') else item.get('description', '')
        worker_resp = responses_by_title.get(title, {})
        reason = worker_resp.get("reason", "No reason provided")

        lines.append(f"### {title}")
        lines.append(f"**Reviewer:** {desc}")
        lines.append(f"**Worker:** {reason}")
        lines.append("")

    lines.append("_A human needs to arbitrate these disputes._")

    try:
        client.add_comment(feature.github_pr_number, "\n".join(lines))
        logger.info("Posted %d escalation(s) to PR #%d", len(escalated_items), feature.github_pr_number)
    except Exception:
        logger.exception("Failed to post escalations to PR #%d", feature.github_pr_number)


def post_pr_link(client: GitHubAppClient, feature) -> None:
    """Post a comment linking the PR when work is complete."""
    if not feature.github_issue_number or not feature.github_pr_url:
        return
    try:
        client.add_comment(feature.github_issue_number,
            f"Fix submitted: {feature.github_pr_url}")
    except Exception:
        logger.exception("Failed to post PR link to issue #%d", feature.github_issue_number)


def sync_description_to_github(client: GitHubAppClient, feature) -> None:
    """Push an updated feature description back to the GitHub issue."""
    if not feature.github_issue_number:
        return
    try:
        client._request(
            "PATCH", f"/repos/{client.repo}/issues/{feature.github_issue_number}",
            json={"body": feature.description},
        )
        client.update_labels(feature.github_issue_number, remove=[NEEDS_INFO_LABEL])
        client.add_comment(feature.github_issue_number,
            "Description updated via kanban UI. Re-triaging.")
        logger.info("Synced description to GitHub issue #%d", feature.github_issue_number)
    except Exception:
        logger.exception("Failed to sync description to issue #%d", feature.github_issue_number)


def _get_latest_user_comment(client: GitHubAppClient, issue_num: int) -> tuple[int | None, str | None]:
    """Get the most recent non-bot comment on an issue. Returns (comment_id, body) or (None, None)."""
    try:
        resp = client._request("GET", f"/repos/{client.repo}/issues/{issue_num}/comments")
        comments = resp.json()
        for comment in reversed(comments):
            user = comment.get("user", {})
            if user.get("type") != "Bot" and user.get("login") != "kanban-agents[bot]":
                return comment["id"], comment.get("body", "")
    except Exception:
        logger.exception("Failed to fetch comments for issue #%d", issue_num)
    return None, None


def check_needs_info_updates(client: GitHubAppClient) -> None:
    """Check if any needs_info issues have been updated on GitHub and re-trigger triage.

    Checks both issue body edits and new comments from the issue author.
    """
    try:
        issues = client.fetch_issues(labels=[NEEDS_INFO_LABEL])
    except Exception:
        logger.exception("Failed to fetch needs-info issues")
        return

    mapping = load_issue_map()

    for issue in issues:
        issue_num = issue["number"]
        bug_id = mapping.get(issue_num)
        if not bug_id:
            continue

        feature = store.load_feature(bug_id)
        if not feature or feature.status != "needs_info":
            continue

        # Check for new user comment we haven't seen yet
        cursors = load_comment_cursors()
        last_seen = cursors.get(issue_num, 0)
        comment_id, user_reply = _get_latest_user_comment(client, issue_num)

        has_new_comment = comment_id is not None and comment_id > last_seen

        # Also check for body edit
        issue_body = (issue.get("body") or "").strip()
        body_changed = issue_body != feature.description.strip()

        if not body_changed and not has_new_comment:
            continue

        # Build updated description from body + any new comment
        new_desc = issue_body
        if has_new_comment and user_reply:
            new_desc += f"\n\nAdditional info from reporter:\n{user_reply}"

        # Track that we've seen this comment
        if comment_id:
            cursors[issue_num] = comment_id
            save_comment_cursors(cursors)

        logger.info("GitHub issue #%d updated for %s, re-triggering triage", issue_num, bug_id)
        feature.description = new_desc
        store.save_feature(feature)

        # Create re-triage task
        from controlplane.models import FeatureStatus, Task
        tid = store._next_task_id()
        task = Task(
            id=tid,
            root_feature_id=feature.id,
            agent="triage",
            type="triage",
            title=f"Re-triage: {feature.title}",
            description=feature.description,
            priority="high",
        )
        store.save_task(task)
        feature.status = FeatureStatus.TRIAGING.value
        feature.current_task_id = tid
        store.save_feature(feature)

        # Clean up labels
        try:
            client.update_labels(issue_num, remove=[NEEDS_INFO_LABEL], add=[ACTIVE_LABEL])
            client.add_comment(issue_num, "Response received. Re-triaging.")
        except Exception:
            logger.exception("Failed to update labels on issue #%d", issue_num)


def close_issue(client: GitHubAppClient, feature) -> None:
    """Close the GitHub issue when the feature is done."""
    if not feature.github_issue_number:
        return
    try:
        pr_note = f" via {feature.github_pr_url}" if feature.github_pr_url else ""
        client.add_comment(feature.github_issue_number,
            f"Resolved{pr_note}. Closing.")
        client.close_issue(feature.github_issue_number)
        logger.info("Closed GitHub issue #%d for %s", feature.github_issue_number, feature.id)
    except Exception:
        logger.exception("Failed to close issue #%d", feature.github_issue_number)


async def poll_github_issues(client: GitHubAppClient) -> None:
    """Background loop that polls GitHub for new issues and needs_info updates."""
    logger.info("GitHub issue poller started (interval=%ds, label=%s)", POLL_INTERVAL, TRIGGER_LABEL)

    while True:
        try:
            # Check for new issues to import
            issues = await asyncio.get_event_loop().run_in_executor(
                None, client.fetch_issues, [TRIGGER_LABEL],
            )
            if issues:
                mapping = load_issue_map()
                for issue in issues:
                    if issue["number"] not in mapping:
                        await asyncio.get_event_loop().run_in_executor(
                            None, import_issue, client, issue,
                        )

            # Check for needs_info issues that were updated on GitHub
            await asyncio.get_event_loop().run_in_executor(
                None, check_needs_info_updates, client,
            )
        except Exception:
            logger.exception("GitHub issue poll failed")

        await asyncio.sleep(POLL_INTERVAL)
