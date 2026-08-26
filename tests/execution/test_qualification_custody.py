from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.execution.qualification_custody import (
    PreAdmissionEngineeringQualificationCustody,
)
from aletheia.execution.runtime_contracts import (
    QualificationAuthorityVerifier,
    QualificationVerificationError,
)

_EXECUTION_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_EXECUTION_TESTS))
from test_runtime_contracts import _Resolver, _qualification_case  # noqa: E402


def test_pre_admission_custody_freshly_recomputes_exact_qualification() -> None:
    case = _qualification_case()
    custody = PreAdmissionEngineeringQualificationCustody(
        authority=QualificationAuthorityVerifier(case.pin),
        artifact_resolver=_Resolver((case.resolution,)),
        execution_authority_resolver=case.authority_resolver,
    )

    verified = custody.verify_engineering_qualification_custody(
        bundle=case.bundle,
        grant=case.grant,
        observed_at=case.observed_at,
    )

    assert verified.bundle_sha256 == case.bundle.bundle_sha256
    assert verified.grant_sha256 == case.grant.grant_sha256
    assert verified.verified_at == case.observed_at
    assert not hasattr(custody, "admit_and_reserve")


def test_pre_admission_custody_rejects_drift_and_cannot_claim_later_admission() -> None:
    case = _qualification_case()
    custody = PreAdmissionEngineeringQualificationCustody(
        authority=QualificationAuthorityVerifier(case.pin),
        artifact_resolver=_Resolver((case.resolution,)),
        execution_authority_resolver=case.authority_resolver,
    )
    with pytest.raises(QualificationVerificationError):
        custody.verify_engineering_qualification_custody(
            bundle=case.bundle,
            grant=case.grant,
            observed_at=case.observed_at + timedelta(hours=2),
        )
    with pytest.raises(QualificationVerificationError, match="cannot assert"):
        custody.verify_qualification_admission(
            qualification_admission_sha256="a" * 64,
            bundle=case.bundle,
            grant=case.grant,
            observed_at=case.observed_at,
        )
