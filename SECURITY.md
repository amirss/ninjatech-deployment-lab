# Security

## Project status

This repository is an independent engineering lab. It is not a hosted service and the
default branch is not suitable for production use.

In particular, it does not yet provide authentication, tenant isolation, production secret
management, or real external-provider integrations.

## Reporting a vulnerability

Do not place credentials, customer data, exploit payloads, or other sensitive details in a
public issue.

Use GitHub's private vulnerability-reporting channel for this repository when it is
available. If that channel is unavailable, open a public issue containing only a short
request for a private contact method.

Please include:

- the affected commit and component;
- the security boundary that can be crossed;
- the minimum safe reproduction description;
- the expected and observed behavior;
- any known mitigation.

## Supported version

Security fixes apply to the current `main` branch. There are no supported production
releases or compatibility guarantees yet.
