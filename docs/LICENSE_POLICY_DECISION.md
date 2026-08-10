# Project license decision

Status date: 2026-08-10

## Current legal posture

FXAlpha currently uses the repository's all-rights-reserved `LICENSE`. A public
GitHub repository under these terms is source-visible and forkable through the
hosting service, but it is not open-source software and does not grant general
rights to use, modify, redistribute, sublicense, or sell FXAlpha.

Third-party submodules are separate works under their own MIT or BSD 3-Clause
terms. Their licenses do not automatically relicense FXAlpha-owned code.

This current posture is internally consistent with `CONTRIBUTING.md`, which
does not accept external code contributions. It is therefore possible to make
the repository public without first adopting an OSI-approved license, provided
the project is described as source-visible rather than open source.

## Decision required before broader participation

Before inviting external code contributions or describing FXAlpha as open
source, the owner should choose and record one of these paths:

1. retain all-rights-reserved terms and continue accepting issue reports and
   documentation feedback only;
2. adopt a permissive license such as Apache-2.0 or MIT and update contribution
   terms;
3. adopt a reciprocal license after reviewing how it interacts with deployment,
   model artifacts, data rights, and the intended commercial model;
4. use a contributor agreement or dual-license structure after legal review.

This document is an engineering handoff, not legal advice. The selected path
must be reflected consistently in `LICENSE`, `README.md`, `CONTRIBUTING.md`,
package metadata, and the GitHub repository description before accepting code.

## Publication decision record

- first public upload: all-rights-reserved unless the owner explicitly replaces
  `LICENSE` before upload
- repository description: use “source-visible” and do not claim “open source”
- external code pull requests: remain closed until contribution-compatible
  terms are adopted
- third-party notices: retain `NOTICE` and all submodule license files
