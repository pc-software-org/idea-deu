"""Deterministic integrity validation for translated IntelliJ strings."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any


class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


class FindingCode(StrEnum):
    PLACEHOLDER_MISMATCH = "placeholder_mismatch"
    MESSAGE_FORMAT_INVALID = "message_format_invalid"
    MARKUP_STRUCTURE_CHANGED = "markup_structure_changed"
    LINK_CHANGED = "link_changed"
    EMPTY_TARGET = "empty_target"
    LENGTH_RATIO = "length_ratio"
    GLOSSARY_MISMATCH = "glossary_mismatch"


@dataclass(frozen=True, slots=True)
class Finding:
    code: FindingCode
    severity: Severity

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "severity": self.severity.value}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    findings: tuple[Finding, ...]

    @property
    def is_blocking(self) -> bool:
        return any(finding.severity is Severity.BLOCKING for finding in self.findings)

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {"findings": [finding.to_dict() for finding in self.findings]}


_PRINTF = re.compile(
    r"%(?:%|(?:\d+\$)?[-#+ 0,(<]*\d*(?:\.\d+)?"
    r"(?:[tT][HIklMSLNpzZsQBbhAaCYyjmdeRTrDFc]|[bBhHsScCdoxXeEfgGaAn]))"
)
_TEMPLATE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_.-]*\}|\$[A-Za-z_][A-Za-z0-9_]*\$")
_TAG_LIKE = re.compile(r"</?[A-Za-z][^<>]*>")
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)


def validate_translation(
    source: str,
    target: str,
    *,
    glossary: Mapping[str, str | Sequence[str]] | None = None,
    context: Mapping[str, Any] | None = None,
) -> ValidationResult:
    """Return blocking integrity errors and non-blocking quality warnings."""
    del context
    findings: list[Finding] = []
    if not target.strip():
        findings.append(Finding(FindingCode.EMPTY_TARGET, Severity.BLOCKING))

    source_markup = _markup(source)
    target_markup = _markup(target)
    source_message = _messages_in_content(source, source_markup)
    target_message = _messages_in_content(target, target_markup)
    message_relevant = source_message.recognized or target_message.recognized
    if message_relevant and not target_message.valid:
        findings.append(Finding(FindingCode.MESSAGE_FORMAT_INVALID, Severity.BLOCKING))

    markup_relevant = source_markup.relevant or target_markup.relevant
    if markup_relevant:
        if not target_markup.valid or source_markup.structure != target_markup.structure:
            findings.append(Finding(FindingCode.MARKUP_STRUCTURE_CHANGED, Severity.BLOCKING))
        elif source_markup.links != target_markup.links:
            findings.append(Finding(FindingCode.LINK_CHANGED, Severity.BLOCKING))

    source_placeholders = _placeholder_multiset(source, source_message)
    target_placeholders = _placeholder_multiset(target, target_message)
    if source_placeholders != target_placeholders:
        findings.append(Finding(FindingCode.PLACEHOLDER_MISMATCH, Severity.BLOCKING))

    if source and len(target) > len(source) * 2.5:
        findings.append(Finding(FindingCode.LENGTH_RATIO, Severity.WARNING))
    if glossary and _violates_glossary(target, glossary):
        findings.append(Finding(FindingCode.GLOSSARY_MISMATCH, Severity.WARNING))
    return ValidationResult(tuple(_deduplicate(findings)))


@dataclass(frozen=True, slots=True)
class _MessageParse:
    tokens: tuple[str, ...]
    valid: bool
    recognized: bool


def _messages_in_content(text: str, markup: _Markup) -> _MessageParse:
    visible_text = _TAG_LIKE.sub("", text) if markup.relevant else text
    parses = [_message_tokens(visible_text)]
    parses.extend(_message_tokens(value) for value in markup.attributes)
    return _MessageParse(
        tuple(token for parsed in parses for token in parsed.tokens),
        all(parsed.valid for parsed in parses),
        any(parsed.recognized for parsed in parses),
    )


def _message_tokens(text: str) -> _MessageParse:
    tokens: list[str] = []
    index = 0
    quoted = False
    valid = True
    recognized = False
    while index < len(text):
        character = text[index]
        if character == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
            index += 1
            continue
        if not quoted and character == "{":
            end = _matching_brace(text, index)
            if end is None:
                valid = False
                break
            content = text[index + 1 : end]
            signature = _message_signature(content)
            if signature is not None:
                recognized = True
                tokens.append(signature)
                nested = _message_tokens(content)
                tokens.extend(nested.tokens)
                valid = valid and nested.valid
                if signature.endswith(":choice"):
                    valid = valid and _valid_choice_style(content)
                recognized = recognized or nested.recognized
            elif re.match(r"\s*\d+\s*(?:,|$)", content):
                recognized = True
                valid = False
            index = end + 1
            continue
        if not quoted and character == "}":
            valid = False
        index += 1
    if quoted:
        valid = False
    return _MessageParse(tuple(tokens), valid, recognized)


def _matching_brace(text: str, start: int) -> int | None:
    depth = 0
    quoted = False
    index = start
    while index < len(text):
        if text[index] == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and text[index] == "{":
            depth += 1
        elif not quoted and text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _message_signature(content: str) -> str | None:
    match = re.match(
        r"\s*(\d+)\s*(?:,\s*(number|date|time|choice)\s*(?:,\s*(.*))?)?$",
        content,
        re.DOTALL,
    )
    if match is None:
        return None
    argument, kind, style = match.groups()
    normalized_style = (style or "").strip()
    return f"mf:{argument}:{kind or ''}:{normalized_style if kind != 'choice' else 'choice'}"


def _valid_choice_style(content: str) -> bool:
    match = re.match(r"\s*\d+\s*,\s*choice\s*,\s*(.*)$", content, re.DOTALL)
    if match is None:
        return False
    style = match.group(1)
    alternatives = _split_choice(style)
    if len(alternatives) == 1 and not any(marker in style for marker in "#<≤"):
        return True  # ChoiceFormat tolerates a pattern without a limit separator.
    previous: float | None = None
    for alternative in alternatives:
        separator = _choice_separator(alternative)
        if separator is None:
            return False
        index, marker = separator
        limit_text = alternative[:index].strip()
        try:
            limit = _choice_limit(limit_text)
        except ValueError:
            return False
        if marker == "<" and math.isfinite(limit):
            limit = math.nextafter(limit, math.inf)
        if previous is not None and limit <= previous:
            return False
        previous = limit
    return True


def _choice_limit(value: str) -> float:
    if value == "∞":
        return float("inf")
    if value == "-∞":
        return float("-inf")
    if re.fullmatch(r"[-+]?(?:NaN|Infinity)", value):
        return float(value)

    suffixless = value[:-1] if value[-1:] in "fFdD" else value
    decimal = r"[-+]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    hexadecimal = (
        r"[-+]?0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?"
        r"|\.[0-9a-fA-F]+)[pP][-+]?\d+"
    )
    if re.fullmatch(decimal, suffixless):
        return float(suffixless)
    if re.fullmatch(hexadecimal, suffixless):
        try:
            return float.fromhex(suffixless)
        except OverflowError:
            return -math.inf if suffixless.startswith("-") else math.inf
    raise ValueError(value)


def _split_choice(style: str) -> list[str]:
    result: list[str] = []
    start = 0
    quoted = False
    depth = 0
    index = 0
    while index < len(style):
        if style[index] == "'":
            if index + 1 < len(style) and style[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and style[index] == "{":
            depth += 1
        elif not quoted and style[index] == "}":
            depth -= 1
        elif not quoted and depth == 0 and style[index] == "|":
            result.append(style[start:index])
            start = index + 1
        index += 1
    result.append(style[start:])
    return result


def _choice_separator(alternative: str) -> tuple[int, str] | None:
    quoted = False
    index = 0
    while index < len(alternative):
        character = alternative[index]
        if character == "'":
            if index + 1 < len(alternative) and alternative[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and character in "#<≤":
            return index, character
        index += 1
    return None


def _placeholder_multiset(text: str, message: _MessageParse) -> Counter[str]:
    tokens = list(message.tokens)
    tokens.extend(_printf_tokens(text))
    tokens.extend(f"template:{match.group()}" for match in _TEMPLATE.finditer(text))
    tokens.extend(_mnemonics(text))
    return Counter(tokens)


def _printf_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _PRINTF.finditer(text):
        token = match.group()
        prefix = text[:match.start()].rstrip()
        prose_percent = (
            token.startswith("% ")
            and bool(prefix)
            and prefix[-1].isdigit()
            and match.end() < len(text)
            and text[match.end()].isascii()
            and text[match.end()].isalpha()
        )
        if not prose_percent:
            tokens.append(f"printf:{token}")
    return tokens


def _mnemonics(text: str) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("&&", index):
            result.append("escaped:&")
            index += 2
        elif (
            text[index] == "&"
            and index + 1 < len(text)
            and text[index + 1].isalnum()
            and (index == 0 or not text[index - 1].isalnum())
        ):
            result.append("mnemonic:&")
            index += 1
        elif text.startswith("__", index):
            result.append("escaped:_")
            index += 2
        elif (
            text[index] == "_"
            and index + 1 < len(text)
            and text[index + 1].isalnum()
            and (index == 0 or not text[index - 1].isalnum())
        ):
            result.append("mnemonic:_")
            index += 1
        index += 1
    return result


@dataclass(frozen=True, slots=True)
class _Markup:
    relevant: bool
    valid: bool
    structure: tuple[tuple[str, ...], ...]
    links: tuple[tuple[str, str, str], ...]
    attributes: tuple[str, ...]


class _FragmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.paths: list[str] = []
        self.child_counts: list[int] = [0]
        self.structure: list[tuple[str, ...]] = []
        self.links: list[tuple[str, str, str]] = []
        self.attributes: list[str] = []
        self.valid = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        child_index = self.child_counts[-1]
        self.child_counts[-1] += 1
        parent_path = self.paths[-1] if self.paths else ""
        path = f"{parent_path}/{tag}[{child_index}]"
        event = "void" if tag in _VOID_TAGS else "start"
        attribute_names = tuple(sorted(name.lower() for name, _value in attrs))
        self.structure.append((event, tag, *attribute_names))
        if event == "start":
            self.stack.append(tag)
            self.paths.append(path)
            self.child_counts.append(0)
        for name, value in attrs:
            self.attributes.append(value or "")
            if name.lower() in {"href", "src"}:
                self.links.append((path, name.lower(), value or ""))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.stack.pop()
            self.paths.pop()
            self.child_counts.pop()
            self.structure[-1] = (
                "void", tag.lower(), *self.structure[-1][2:]
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
            return
        self.stack.pop()
        self.paths.pop()
        self.child_counts.pop()
        self.structure.append(("end", tag))

    def handle_comment(self, data: str) -> None:
        self.structure.append(("comment",))

    def handle_pi(self, data: str) -> None:
        self.structure.append(("pi",))

    def handle_decl(self, decl: str) -> None:
        self.valid = False

    def handle_entityref(self, name: str) -> None:
        if name not in {"amp", "lt", "gt", "quot", "apos", "nbsp"}:
            self.valid = False


def _markup(text: str) -> _Markup:
    relevant = bool(
        _TAG_LIKE.search(text)
        or "<!DOCTYPE" in text.upper()
        or "<!--" in text
        or "<?" in text
    )
    if not relevant:
        return _Markup(False, True, (), (), ())
    parser = _FragmentParser()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError):
        parser.valid = False
    return _Markup(
        True,
        parser.valid and not parser.stack,
        tuple(parser.structure),
        tuple(sorted(parser.links)),
        tuple(parser.attributes),
    )


def _violates_glossary(target: str, glossary: Mapping[str, str | Sequence[str]]) -> bool:
    for preferred, raw_variants in glossary.items():
        variants = (raw_variants,) if isinstance(raw_variants, str) else raw_variants
        for variant in variants:
            if variant.casefold() == preferred.casefold():
                continue
            pattern = rf"(?<!\w){re.escape(variant)}(?!\w)"
            if re.search(pattern, target, re.IGNORECASE):
                return True
    return False


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    result: list[Finding] = []
    seen: set[FindingCode] = set()
    for finding in findings:
        if finding.code not in seen:
            result.append(finding)
            seen.add(finding.code)
    return result
