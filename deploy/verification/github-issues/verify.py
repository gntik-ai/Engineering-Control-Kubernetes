"""One operator-requested issue permission check through the installed Engineering Control App."""

import asyncio
import os
import sys
from uuid import UUID

from engineering_control.config import get_settings
from engineering_control.github_app import GitHubApp, GitHubAppError


async def verify() -> None:
    repository = os.environ["VERIFY_REPOSITORY"]
    identifier = str(UUID(os.environ["VERIFY_ID"]))
    if os.environ.get("CONFIRM_CREATE_AND_CLOSE") != repository:
        raise ValueError("A matching repository confirmation is required")
    marker = f"<!-- engineering-control:permission-check:{identifier} -->"
    title = "[Engineering Control verification] GitHub App issue permissions"
    details = (
        "The repository administrator requested this installation check. "
        "Engineering Control closes this verification issue after reading it back.\n\n" + marker
    )
    app = GitHubApp.from_settings(get_settings())
    if app is None:
        raise ValueError("Configure the product's GitHub App before checking its installation")
    try:
        token = await app.repository_token(repository, {"issues": "write"})
        try:
            path = f"/repos/{repository}/issues"
            # A retry can finish the same check without reopening or closing another issue.
            # The check ID is unique to this invocation and is never taken from GitHub.
            page = 1
            found = None
            while found is None:
                issues = await app._request(
                    "GET",
                    path,
                    token=token,
                    params={
                        "state": "all",
                        "per_page": 100,
                        "page": page,
                        "sort": "updated",
                    },
                )
                for issue in issues:
                    if (
                        "pull_request" not in issue
                        and issue["title"] == title
                        and issue.get("body") == details
                        and issue["user"]["type"] == "Bot"
                    ):
                        if (issue.get("performed_via_github_app") or {}).get("id") != app.app_id:
                            raise RuntimeError("Existing check needs operator reconciliation")
                        found = issue
                        break
                if len(issues) < 100:
                    break
                page += 1
            if found is None:
                found = await app._request(
                    "POST", path, token=token, json={"title": title, "body": details}
                )
            number = int(found["number"])
            specific = f"{path}/{number}"
            result = await app._request("GET", specific, token=token)
            if (
                result.get("body") != details
                or result.get("title") != title
                or result.get("user", {}).get("type") != "Bot"
                or "pull_request" in result
            ):
                raise RuntimeError("Verification issue identity did not match")
            if result.get("state") != "closed":
                await app._request(
                    "PATCH",
                    specific,
                    token=token,
                    json={"state": "closed", "state_reason": "not_planned"},
                )
            final = await app._request("GET", specific, token=token)
            if final.get("state") != "closed" or final.get("body") != details:
                raise RuntimeError("The issue closure could not be verified")
            print(
                f"Verified https://github.com/{repository}/issues/{number} "
                f"created by {final['user']['login']}, read back and closed."
            )
        finally:
            await app.revoke(token)
    finally:
        await app.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(verify())
    except (GitHubAppError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
