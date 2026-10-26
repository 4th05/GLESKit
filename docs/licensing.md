# Non-commercial, share-alike licensing

Original GLESKit materials use **Creative Commons Attribution-NonCommercial-
ShareAlike 4.0 International (CC BY-NC-SA 4.0)**. The complete standard legal text
is in [LICENSE](../LICENSE), unchanged from Creative Commons. The project
copyright and license application notice are in [NOTICE](../NOTICE).

Anyone may use the material for non-commercial purposes; permission is not
limited to academics. Commercial purposes are not permitted by this license.
The official terms control; this guide does not add conditions to them.

## Contents

- [What the standard terms require](#what-the-standard-terms-require)
- [Scope](#scope)
- [Release review](#release-review)

## What the standard terms require

- Attribution: retain the supplied creator/copyright/license notices and identify
  modifications when sharing material, as specified in Section 3(a).
- NonCommercial: the use must not be primarily intended for or directed toward
  commercial advantage or monetary compensation, as defined in Section 1(k).
  The purpose of the use matters; a person's employer or nonprofit status alone
  does not determine permission.
- ShareAlike: when sharing an adaptation, apply a CC license with the same
  license elements, version 4.0 or later, or an approved BY-NC-SA compatible
  license, as specified in Section 3(b).
- No additional restrictions: do not impose legal or effective technological
  restrictions that prevent exercise of the rights the license grants.

Private modifications need not be published. ShareAlike does not impose a
software-specific obligation to supply editable source or disclose server-side
modifications merely because software is hosted. No such extra condition is
added here. The license does not claim ownership of users' research data or
results; separately identified third-party materials retain their own terms.

## Scope

The grant covers original GLESKit materials the author has the right to license.
It does not relicense dependencies, public datasets, models, or other upstream
materials. Review [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

Separately licensed publications and earlier releases retain their existing
permissions; this repository's license does not revoke those grants.

Creative Commons recommends against CC licenses for software because they lack
software-specific source-distribution provisions and can create compatibility
issues with software licenses. GLESKit uses the standard non-commercial,
share-alike terms without adding custom source-disclosure restrictions. The
non-commercial restriction means the code is source available, not OSI-approved
open source. Third-party compatibility and journal requirements still need review.

Official references:

- [CC BY-NC-SA 4.0 overview](https://creativecommons.org/licenses/by-nc-sa/4.0/).
- [Unmodified legal text](https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.txt).
- [CC guidance for software](https://creativecommons.org/faq/#can-i-apply-a-creative-commons-license-to-software).
- [CC NonCommercial guidance](https://creativecommons.org/faq/#does-my-use-violate-the-noncommercial-clause-of-the-licenses).
- [Existing CC grants are irrevocable](https://creativecommons.org/faq/#what-if-i-change-my-mind-about-using-a-cc-license).

## Release review

The source-license selection is recorded in `docs/licensing-review.json`.
The review boundary is the source-only GitHub snapshot: original source/branding,
documentation and pinned public dependency download recipes. It excludes installed
packages, compiled bundles, databases, datasets and prebuilt images. Its dependency
inventory/notice review is separate from binary-image redistribution review. This
is a technical release review, not legal certification or approval of downstream
uses. Do not mark it complete merely because tests pass or metadata lists a license.

After the exact third-party/content review, complete `reviewed_sha256` for every
input returned by `review_inputs()` in `scripts/release/licensing_gate.py` and
set `third_party_review_complete` to `true`. This includes the license, attribution
notice, dependency inventory, package metadata/lockfile, R runner, and all R
scripts. Run `make release_license_check`. Changed inputs invalidate the review;
the standard license text must retain its verified official SHA-256 identity.

Docker instructions/locks remain hash-bound source-review inputs; their presence
does not itself distribute downloaded runtimes. The separate image check is:

```sh
python3 scripts/release/licensing_gate.py --distribution image
```

It additionally requires `container_review_complete`, which stays false for a
source-only release. Do not publish a prebuilt image without its corresponding-
source, license-text and notice obligations reviewed. GLESKit's non-commercial
terms must not be applied to independently licensed upstream components. Their
licenses continue to control local installations and any downstream redistribution.

The check also applies to the selected committed bytes during clean source
export. Privacy, content, clean-machine, and publication approval are separate
requirements. The check does not block local UI startup, tests, or builds and
cannot intercept a manual `git push`. Never push private development history.
