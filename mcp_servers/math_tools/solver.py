from collections.abc import Mapping, Sequence
from enum import StrEnum

import sympy
from sympy import Expr

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import (
    ParsedEquation,
    build_equation_syntax,
    build_variable,
)
from mcp_servers.math_tools.limits import MAX_SOLUTIONS
from mcp_servers.math_tools.payloads import SolveRequest
from mcp_servers.math_tools.schemas import MathSolveResult, SolveStatus
from mcp_servers.math_tools.symbolic import (
    checked_text,
    constraints_hold,
    is_real_value,
    is_zero_expression,
    numeric_text,
    substitute,
)


class SolveClassification(StrEnum):
    FINITE = "finite"
    INFINITE = "infinite_solutions"
    UNDEFINED = "undefined"


def solve(request: SolveRequest) -> dict[str, object]:
    equations = [build_equation_syntax(equation) for equation in request.equations]
    variables = list(request.variables)
    symbols = [build_variable(name) for name in variables]
    display_equations = [equation.normalized for equation in equations]

    candidates, classification = _solve_candidates(
        equations,
        symbols,
        request.real_only,
    )
    if classification is SolveClassification.INFINITE:
        return _solve_payload(
            status=SolveStatus.INFINITE_SOLUTIONS,
            equations=display_equations,
            variables=variables,
            real_only=request.real_only,
            detail="Уравнение имеет бесконечно много решений; уточните ограничения.",
        )
    if classification is SolveClassification.UNDEFINED:
        return _solve_payload(
            status=SolveStatus.UNDEFINED,
            equations=display_equations,
            variables=variables,
            real_only=request.real_only,
            detail="Не удалось однозначно определить множество решений.",
        )

    if len(candidates) > MAX_SOLUTIONS:
        raise MathError("resource_limit", "Слишком много решений для выдачи.")

    accepted: list[dict[str, str]] = []
    numeric: list[dict[str, str]] = []
    discarded: list[dict[str, str]] = []

    for candidate in candidates:
        exact_solution = {
            variable: checked_text(sympy.sstr(candidate[symbol]))
            for variable, symbol in zip(variables, symbols, strict=True)
        }
        if not _verify_solution(equations, candidate, symbols, request.real_only):
            discarded.append(exact_solution)
            continue

        accepted.append(exact_solution)
        numeric.append(
            {
                variable: checked_text(numeric_text(candidate[symbol]))
                for variable, symbol in zip(variables, symbols, strict=True)
            }
        )

    accepted, numeric = _sort_solutions(accepted, numeric, variables)
    if accepted:
        return _solve_payload(
            status=SolveStatus.SOLUTIONS,
            equations=display_equations,
            variables=variables,
            real_only=request.real_only,
            solutions=accepted,
            numeric_solutions=numeric,
            discarded=discarded,
            verified=True,
            detail=(
                "Все решения проверены подстановкой в исходные уравнения и по области определения."
            ),
        )

    detail = "Вещественных решений нет." if request.real_only else "Решений нет."
    if discarded:
        detail = (
            "Кандидаты решений не прошли проверку по исходным уравнениям или области определения."
        )
    return _solve_payload(
        status=SolveStatus.NO_SOLUTION,
        equations=display_equations,
        variables=variables,
        real_only=request.real_only,
        discarded=discarded,
        detail=detail,
    )


def _solve_candidates(
    equations: Sequence[ParsedEquation],
    symbols: Sequence[sympy.Symbol],
    real_only: bool,
) -> tuple[list[dict[sympy.Symbol, Expr]], SolveClassification]:
    if len(equations) == 1 and len(symbols) == 1:
        domain = sympy.S.Reals if real_only else sympy.S.Complexes
        try:
            solution_set = sympy.solveset(equations[0].difference, symbols[0], domain=domain)
        except (NotImplementedError, ValueError):
            solution_set = None
        finite_values = _finite_set_values(solution_set)
        if finite_values is not None:
            return (
                [{symbols[0]: value} for value in finite_values],
                SolveClassification.FINITE,
            )
        if solution_set is None or isinstance(solution_set, sympy.ConditionSet):
            return [], SolveClassification.UNDEFINED
        return [], SolveClassification.INFINITE

    try:
        raw_solutions = sympy.solve(
            [equation.difference for equation in equations],
            list(symbols),
            dict=True,
        )
    except (NotImplementedError, ValueError, TypeError):
        return [], SolveClassification.UNDEFINED

    if isinstance(raw_solutions, dict):
        raw_solutions = [raw_solutions]
    if not isinstance(raw_solutions, list):
        return [], SolveClassification.UNDEFINED
    if not raw_solutions:
        return [], SolveClassification.FINITE

    candidates: list[dict[sympy.Symbol, Expr]] = []
    for raw_solution in raw_solutions:
        if not isinstance(raw_solution, dict):
            return [], SolveClassification.UNDEFINED
        candidate: dict[sympy.Symbol, Expr] = {}
        for symbol in symbols:
            value = raw_solution.get(symbol)
            if value is None:
                return [], SolveClassification.UNDEFINED
            if value.free_symbols & set(symbols):
                return [], SolveClassification.INFINITE
            candidate[symbol] = value
        candidates.append(candidate)
    return candidates, SolveClassification.FINITE


def _finite_set_values(solution_set: object) -> list[Expr] | None:
    if solution_set is None:
        return None
    if solution_set == sympy.S.EmptySet:
        return []
    if isinstance(solution_set, sympy.FiniteSet):
        return [value for value in solution_set if isinstance(value, Expr)]
    if isinstance(solution_set, sympy.Union):
        values: list[Expr] = []
        for part in solution_set.args:
            if not isinstance(part, sympy.FiniteSet):
                return None
            values.extend(value for value in part if isinstance(value, Expr))
        return values
    return None


def _verify_solution(
    equations: Sequence[ParsedEquation],
    solution: Mapping[sympy.Symbol, Expr],
    symbols: Sequence[sympy.Symbol],
    real_only: bool,
) -> bool:
    if real_only:
        for symbol in symbols:
            if not is_real_value(solution[symbol]):
                return False

    substitutions = {symbol: solution[symbol] for symbol in symbols}
    for equation in equations:
        constraints = equation.left.constraints + equation.right.constraints
        if not constraints_hold(constraints, substitutions, real_only):
            return False

        original_residual = sympy.Add(
            equation.left.original,
            sympy.Mul(sympy.Integer(-1), equation.right.original, evaluate=False),
            evaluate=False,
        )
        if not is_zero_expression(substitute(original_residual, substitutions)):
            return False

    return True


def _sort_solutions(
    solutions: list[dict[str, str]],
    numeric: list[dict[str, str]],
    variables: Sequence[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    indexed = list(zip(solutions, numeric, strict=True))
    indexed.sort(key=lambda pair: tuple(pair[0][variable] for variable in variables))
    return [pair[0] for pair in indexed], [pair[1] for pair in indexed]


def _solve_payload(
    *,
    status: SolveStatus,
    equations: Sequence[str],
    variables: Sequence[str],
    real_only: bool,
    solutions: Sequence[Mapping[str, str]] = (),
    numeric_solutions: Sequence[Mapping[str, str]] = (),
    discarded: Sequence[Mapping[str, str]] = (),
    verified: bool = False,
    detail: str,
) -> dict[str, object]:
    return MathSolveResult(
        status=status,
        equations=list(equations),
        variables=list(variables),
        solutions=[dict(solution) for solution in solutions],
        numeric_solutions=[dict(solution) for solution in numeric_solutions],
        discarded=[dict(solution) for solution in discarded],
        real_only=real_only,
        verified=verified,
        detail=detail,
    ).model_dump(mode="json")
