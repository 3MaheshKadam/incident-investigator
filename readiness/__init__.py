"""Production-readiness checks for a repository: Dockerfile, compose, CI workflows, env, secrets.

Each check is a small rule with an id, a severity and a fix hint. The point is not to replace
tools like hadolint or actionlint, but to have a transparent rule set that is easy to explain,
extend and test, and whose findings the platform can later hand to the AI to explain and rank.
"""
