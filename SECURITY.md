# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Instead, use GitHub's private
reporting: go to this repository's **Security** tab → **Report a vulnerability**.

Include what you found, how to reproduce it, and the impact you expect. You'll get an
acknowledgement within a few days.

## Supported versions

Only the latest commit on `main` is supported.

## Data in this repository

All data in this repository is **synthetic**. Demo users use `example.com` addresses and get
random passwords generated on your machine when you run `experiment-ledger seed`; experiments
and their results are fictional. No real people, customers or companies are represented.

The repository contains no credentials. Every secret is supplied at runtime through environment
variables, and the application refuses to start without them.
