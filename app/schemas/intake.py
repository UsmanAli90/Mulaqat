"""Intake question definitions.

Per the agreed amendment, intake questions live in a JSONB column on
`services` rather than in their own table — there is only one shape and one
consumer, so a table would be structure without benefit. The tradeoff is that
JSONB accepts literally anything, so these models are what stop the column
from becoming a junk drawer: they are enforced on every write by the
`IntakeQuestions` column type in `app/db/types.py`.

The `key` here is what ends up in `intake_responses.question_key` when an
invitee answers, which is why it is constrained to a stable slug rather than
free text: renaming a label must not orphan existing answers.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


class IntakeQuestionType(StrEnum):
    """Answer widget, and therefore the shape of the stored answer.

    StrEnum members compare equal to their string value, so these serialise to
    plain JSON strings without any custom encoder.
    """

    SHORT_TEXT = "short_text"
    LONG_TEXT = "long_text"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"


_CHOICE_TYPES = {IntakeQuestionType.SINGLE_SELECT, IntakeQuestionType.MULTI_SELECT}


class IntakeQuestion(BaseModel):
    """One question on a service's intake form."""

    # extra="forbid" so a typo'd field is a loud error rather than silently
    # persisted and then silently ignored on read.
    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,49}$",
        description="Stable identifier stored on the answer. Never reuse a key for a new meaning.",
    )
    label: str = Field(min_length=1, max_length=300)
    type: IntakeQuestionType
    required: bool = False
    options: list[str] | None = None
    help_text: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_options_match_type(self) -> Self:
        """Cross-field validation.

        `mode="after"` runs once every field has been parsed, so the whole
        object is available — this is where rules spanning two fields belong.
        The Rails analogue is a `validate :method` that inspects several
        attributes, except a failure here means the object never exists at all
        rather than existing in an invalid state.
        """
        if self.type in _CHOICE_TYPES:
            if not self.options:
                raise ValueError(f"{self.type.value} question {self.key!r} needs options")
            if len(set(self.options)) != len(self.options):
                raise ValueError(f"question {self.key!r} has duplicate options")
        elif self.options is not None:
            raise ValueError(f"{self.type.value} question {self.key!r} must not define options")
        return self


class IntakeQuestionSet(RootModel[list[IntakeQuestion]]):
    """A service's full question list.

    RootModel wraps a bare list so the collection itself can carry validation
    (here: key uniqueness) that no individual question could enforce on its
    own. Access the list through `.root`.
    """

    root: list[IntakeQuestion] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def check_keys_are_unique(self) -> Self:
        keys = [question.key for question in self.root]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(f"duplicate intake question keys: {sorted(duplicates)}")
        return self
