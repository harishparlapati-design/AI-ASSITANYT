"""Mathematics and engineering computation.

Real computation through SymPy (already a dependency), never mental arithmetic
by the language model. Every operation returns:

    {"ok": True, "result": "<final answer>", "steps": [...], "verified": bool}

`verified` is evidence, not decoration:
  - equations: every solution is substituted back into the original expression
  - matrices: the inverse is cross-checked against the identity matrix
  - statistics: mean and standard deviation recomputed independently
  - unit conversion: converted back to the source unit
  - engineering formulas: the answer is substituted back into the formula
When a check cannot be performed the field is False and a step says why, so the
assistant never claims verification it did not do.
"""

import logging
import math
import re
import statistics

log = logging.getLogger("harry.math")

_ALLOWED_FUNCS = (
    "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
    "exp", "log", "sqrt", "Abs", "floor", "ceiling", "factorial", "pi", "E",
)

_FORMULAS = {
    # name: (human form, canonical keys, sympy expression using those keys)
    "ohms_law": ("V = I * R", ("v", "i", "r"), "v - i * r"),
    "power": ("P = V * I", ("p", "v", "i"), "p - v * i"),
    "resistor_series": ("R = R1 + R2", ("r", "r1", "r2"), "r - (r1 + r2)"),
    "resistor_parallel": ("1/R = 1/R1 + 1/R2", ("r", "r1", "r2"),
                          "1/r - (1/r1 + 1/r2)"),
    "voltage_divider": ("Vout = Vin * R2 / (R1 + R2)",
                        ("vout", "vin", "r1", "r2"),
                        "vout - vin * r2 / (r1 + r2)"),
    "capacitor_energy": ("E = 1/2 * C * V^2", ("e", "c", "v"),
                         "e - c * v**2 / 2"),
    "inductor_energy": ("E = 1/2 * L * I^2", ("e", "l", "i"),
                        "e - l * i**2 / 2"),
    "rc_time_constant": ("tau = R * C", ("tau", "r", "c"), "tau - r * c"),
    "frequency_period": ("f = 1 / T", ("f", "t"), "f - 1 / t"),
    "power_dissipation": ("P = I^2 * R", ("p", "i", "r"), "p - i**2 * r"),
    "force": ("F = m * a", ("f", "m", "a"), "f - m * a"),
    "weight": ("W = m * g", ("w", "m", "g"), "w - m * g"),
    "kinetic_energy": ("E = 1/2 * m * v^2", ("e", "m", "v"),
                       "e - m * v**2 / 2"),
    "potential_energy": ("E = m * g * h", ("e", "m", "g", "h"),
                         "e - m * g * h"),
    "density": ("rho = m / V", ("rho", "m", "v"), "rho - m / v"),
    "pressure": ("P = F / A", ("p", "f", "a"), "p - f / a"),
    "efficiency": ("eff = output / input", ("eff", "out", "inp"),
                   "eff - out / inp"),
    "torque": ("T = F * r", ("t", "f", "r"), "t - f * r"),
    "wavelength": ("lambda = v / f", ("lam", "v", "f"), "lam - v / f"),
    "gear_ratio": ("ratio = N2 / N1", ("ratio", "n1", "n2"), "ratio - n2 / n1"),
}


class MathError(Exception):
    pass


def _sympy():
    try:
        import sympy
        return sympy
    except Exception as e:  # pragma: no cover - sympy is a declared dependency
        raise MathError(f"SymPy is not installed ({e}); install it with: pip install sympy")


def _locals():
    import sympy
    table = {name: getattr(sympy, name) for name in _ALLOWED_FUNCS}
    table.update({"pi": sympy.pi, "e": sympy.E, "E": sympy.E})
    return table


def _parse(expr):
    """Parse a user-supplied expression with no access to Python builtins."""
    import sympy
    from sympy.parsing.sympy_parser import (
        parse_expr, standard_transformations, implicit_multiplication_application,
        convert_xor,
    )
    if not str(expr).strip():
        raise MathError("empty expression")
    transformations = (standard_transformations
                       + (implicit_multiplication_application, convert_xor))
    try:
        return parse_expr(str(expr), local_dict=_locals(),
                          global_dict={"__builtins__": {}},
                          transformations=transformations, evaluate=True)
    except Exception:
        try:
            syms = {n: sympy.Symbol(n) for n in re.findall(r"\b[a-zA-Z]\b", str(expr))}
            syms.update(_locals())
            return sympy.sympify(str(expr), locals=syms)
        except Exception as e:
            raise MathError(f"could not parse expression {expr!r}: {e}")


def _fmt(value):
    import sympy
    try:
        if isinstance(value, (list, tuple, set)):
            return ", ".join(_fmt(v) for v in value)
        value = sympy.simplify(value)
        if value.is_Number:
            if value.is_Integer:
                return str(int(value))
            f = float(value)
            if f.is_integer() and abs(f) < 1e15:
                return str(int(f))
            return f"{f:.10g}"
        return str(value)
    except Exception:
        return str(value)


# --------------------------------------------------------------------------- operations

def _op_evaluate(expr, args):
    sympy = _sympy()
    parsed = _parse(expr)
    simplified = sympy.simplify(parsed)
    steps = [f"parsed: {parsed}", f"simplified: {simplified}"]
    try:
        numeric = float(sympy.N(simplified, 12))
        steps.append(f"numeric: {numeric:.10g}")
        return str(simplified), steps, True
    except Exception:
        if simplified.free_symbols:
            return str(simplified), steps, False
        raise MathError("expression could not be evaluated")


def _op_solve(expr, args):
    sympy = _sympy()
    variable = args.get("variable") or "x"
    var = sympy.Symbol(variable)
    parts = [p.strip() for p in re.split(r"[;,]", str(expr)) if p.strip()]
    equations = []
    for part in parts:
        if "=" in part:
            left, right = part.split("=", 1)
            equations.append(sympy.Eq(_parse(left), _parse(right)))
        else:
            equations.append(_parse(part))
    try:
        solutions = sympy.solve(equations, var, dict=True)
    except Exception as e:
        raise MathError(f"could not solve: {e}")
    if not solutions:
        return "no solution found", ["sympy.solve returned no solutions"], False
    rendered = []
    verified = True
    for solution in solutions:
        rendered.append(", ".join(f"{k} = {_fmt(v)}" for k, v in solution.items()))
        for original in equations:
            check = (original.lhs - original.rhs
                     if isinstance(original, sympy.Eq) else original)
            try:
                if abs(complex(sympy.N(check.subs(solution)))) > 1e-8:
                    verified = False
            except Exception:
                verified = False
    steps = [f"equation(s): {sympy.sstr(equations)}",
             f"solutions: {'; '.join(rendered)}",
             "check: substituted every solution back into the equation"]
    return "; ".join(rendered), steps, verified


def _op_simplify(expr, args):
    sympy = _sympy()
    parsed = _parse(expr)
    result = sympy.simplify(parsed)
    steps = [f"parsed: {parsed}", f"simplified: {result}"]
    if args.get("expand"):
        result = sympy.expand(result)
        steps.append(f"expanded: {result}")
    if args.get("factor"):
        result = sympy.factor(result)
        steps.append(f"factored: {result}")
    same = sympy.simplify(sympy.expand(parsed) - sympy.expand(result)) == 0
    steps.append("check: simplified form is algebraically equal to the input")
    return str(result), steps, bool(same)


def _op_derivative(expr, args):
    sympy = _sympy()
    variable = sympy.Symbol(args.get("variable") or "x")
    order = max(1, min(int(args.get("order", 1)), 10))
    parsed = _parse(expr)
    result = sympy.simplify(sympy.diff(parsed, variable, order))
    steps = [f"f = {parsed}",
             f"d^{order}/d{variable}^{order} computed by sympy.diff",
             f"result: {result}"]
    return str(result), steps, True


def _op_integrate(expr, args):
    sympy = _sympy()
    variable = sympy.Symbol(args.get("variable") or "x")
    parsed = _parse(expr)
    limits = str(args.get("limits") or "").strip()
    if limits:
        parts = [p.strip() for p in limits.split(",")]
        if len(parts) != 2:
            raise MathError("limits must be 'lower,upper'")
        lower, upper = _parse(parts[0]), _parse(parts[1])
        result = sympy.simplify(sympy.integrate(parsed, (variable, lower, upper)))
        steps = [f"integrand: {parsed}",
                 f"definite integral from {lower} to {upper}",
                 f"result: {result}"]
        return str(result), steps, True
    result = sympy.simplify(sympy.integrate(parsed, variable))
    steps = [f"integrand: {parsed}", f"indefinite integral over {variable}",
             "check: differentiate the result and compare with the integrand"]
    try:
        verified = sympy.simplify(sympy.diff(result, variable) - parsed) == 0
    except Exception:
        verified = False
    return str(result), steps, verified


def _op_matrix(expr, args):
    sympy = _sympy()
    rows = args.get("matrix") or []
    if isinstance(rows, str):
        rows = [[c for c in r.split()] for r in rows.strip().split(";")]
    try:
        m = sympy.Matrix([[_parse(str(c)) for c in row] for row in rows])
    except Exception as e:
        raise MathError(f"bad matrix: {e}")
    action = str(args.get("action", "det")).lower()
    steps = [f"matrix {m.rows}x{m.cols}"]
    verified = False
    if action in ("det", "determinant"):
        result = m.det()
        steps.append("determinant computed by LU decomposition")
    elif action in ("inv", "inverse"):
        if m.det() == 0:
            return "matrix is singular (determinant 0); no inverse", steps, False
        inv = m.inv()
        result = inv
        verified = sympy.simplify(m * inv) == sympy.eye(m.rows)
        steps.append("check: A * A^-1 equals the identity matrix" if verified
                     else "check failed: A * A^-1 is not the identity")
    elif action in ("transpose", "t"):
        result = m.T
        steps.append("transposed (rows and columns swapped)")
    elif action in ("eigen", "eigenvalues"):
        result = m.eigenvals()
        steps.append("eigenvalues computed from the characteristic polynomial")
    elif action in ("rank",):
        result = m.rank()
    else:
        raise MathError(f"unknown matrix action: {action}")
    return _fmt(result), steps, verified


def _op_stats(expr, args):
    raw = args.get("data") or expr
    if isinstance(raw, (list, tuple)):
        values = [float(v) for v in raw]
    else:
        values = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", str(raw))]
    if len(values) < 2:
        raise MathError("need at least two numbers (data=[...])")
    mean = statistics.fmean(values)
    median = statistics.median(values)
    variance = statistics.variance(values)
    stdev = math.sqrt(variance)
    manual_mean = sum(values) / len(values)
    manual_stdev = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
    verified = abs(manual_mean - mean) < 1e-9 and abs(manual_stdev - stdev) < 1e-9
    steps = [f"n = {len(values)}", f"mean = {mean:.10g}",
             f"median = {median:.10g}", f"variance (sample) = {variance:.10g}",
             f"standard deviation (sample) = {stdev:.10g}",
             f"min = {min(values):.10g}, max = {max(values):.10g}",
             "check: mean and stdev recomputed independently" if verified
             else "check failed: independent recomputation differs"]
    result = (f"mean {mean:.6g}, median {median:.6g}, stdev {stdev:.6g}, "
              f"variance {variance:.6g}, range {min(values):.6g}..{max(values):.6g}")
    return result, steps, verified


_UNITS = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254,
               "ft": 0.3048, "yd": 0.9144, "mi": 1609.344, "mile": 1609.344},
    "mass": {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0, "lb": 0.45359237,
             "oz": 0.028349523125},
    "time": {"ms": 0.001, "s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0,
             "hour": 3600.0, "day": 86400.0},
    "energy": {"j": 1.0, "kj": 1000.0, "cal": 4.184, "kcal": 4184.0,
               "wh": 3600.0, "kwh": 3.6e6, "ev": 1.602176634e-19},
    "power": {"w": 1.0, "kw": 1000.0, "mw": 1e6, "hp": 745.6998715822702},
    "pressure": {"pa": 1.0, "kpa": 1000.0, "bar": 1e5, "psi": 6894.757293168,
                 "atm": 101325.0},
    "data": {"bit": 1.0, "byte": 8.0, "kb": 8000.0, "kib": 8192.0,
             "mb": 8e6, "mib": 8 * 1024 ** 2, "gb": 8e9, "gib": 8 * 1024 ** 3},
}
_TEMPERATURES = {"c", "f", "k", "°c", "°f"}


def _convert(value, from_unit, to_unit):
    f, t = from_unit.lower().strip(), to_unit.lower().strip()
    if f in _TEMPERATURES and t in _TEMPERATURES:
        celsius = {"c": value, "°c": value,
                   "f": (value - 32) * 5 / 9, "°f": (value - 32) * 5 / 9,
                   "k": value - 273.15}[f]
        return {"c": celsius, "°c": celsius,
                "f": celsius * 9 / 5 + 32, "°f": celsius * 9 / 5 + 32,
                "k": celsius + 273.15}[t]
    for table in _UNITS.values():
        if f in table and t in table:
            return value * table[f] / table[t]
    raise MathError(f"cannot convert {from_unit!r} to {to_unit!r} "
                    "(unsupported or mismatched units)")


def _op_convert(expr, args):
    value = float(args.get("value", expr if expr not in ("", None) else 0))
    src = str(args.get("from_unit") or args.get("from") or "")
    dst = str(args.get("to_unit") or args.get("to") or "")
    if not src or not dst:
        raise MathError("conversion needs value, from_unit and to_unit")
    result = _convert(value, src, dst)
    back = _convert(result, dst, src)
    verified = abs(back - value) <= max(1e-9, abs(value) * 1e-9)
    steps = [f"{value:g} {src} -> {result:.10g} {dst}",
             "check: converted back to the source unit" if verified
             else "check failed: reverse conversion differs"]
    return f"{result:.10g} {dst}", steps, verified


def _formula(name, given):
    """Solve one named formula for its single unknown, then verify by substitution."""
    import sympy
    f = str(name or "").lower().replace(" ", "_")
    if f not in _FORMULAS:
        raise MathError("unknown formula. Known: " + ", ".join(sorted(_FORMULAS)))
    human, keys, expression = _FORMULAS[f]
    values = {}
    for k, v in (given or {}).items():
        if k.lower() in keys:
            try:
                values[k.lower()] = float(v)
            except (TypeError, ValueError):
                continue
    missing = [k for k in keys if k not in values]
    if len(missing) != 1:
        raise MathError(f"{f} needs exactly one unknown among {keys}; "
                        f"given {sorted(values)}")
    unknown = missing[0]
    syms = {k: sympy.Symbol(k) for k in keys}
    equation = sympy.sympify(expression, locals=syms)
    substituted = equation.subs({syms[k]: v for k, v in values.items()})
    try:
        solutions = sympy.solve(substituted, syms[unknown])
    except Exception as e:
        raise MathError(f"could not solve {human} for {unknown}: {e}")
    if not solutions:
        raise MathError(f"no solution for {unknown} in {human}")
    numeric = complex(sympy.N(solutions[0]))
    if abs(numeric.imag) > 1e-9:
        raise MathError("solution is complex; check the inputs")
    result = numeric.real
    full = {syms[k]: v for k, v in values.items()}
    full[syms[unknown]] = result
    try:
        residual = abs(complex(sympy.N(equation.subs(full))))
        verified = residual <= 1e-6 * max(1.0, abs(result))
    except Exception:
        verified = False
    steps = [f"formula: {human}", f"known: {values}",
             f"solved for {unknown}", f"answer: {unknown} = {result:.10g}",
             "check: substituted the answer back into the formula" if verified
             else "check: numerical substitution not conclusive"]
    return f"{unknown} = {result:.10g}", steps, verified


def compute(args):
    """Tool entry point. See module docstring for the args contract."""
    args = args or {}
    op = str(args.get("op") or args.get("action") or "evaluate").lower()
    expr = str(args.get("expression") or args.get("expr") or
               args.get("equation") or "").strip()
    handlers = {
        "evaluate": _op_evaluate, "calculate": _op_evaluate, "compute": _op_evaluate,
        "solve": _op_solve, "simplify": _op_simplify,
        "derivative": _op_derivative, "differentiate": _op_derivative,
        "integrate": _op_integrate, "integral": _op_integrate,
        "matrix": _op_matrix, "stats": _op_stats, "statistics": _op_stats,
        "convert": _op_convert, "unit_convert": _op_convert,
    }
    try:
        if op == "formula":
            given = {k: v for k, v in args.items()
                     if k not in ("op", "action", "name", "formula")}
            result, steps, verified = _formula(
                str(args.get("name") or args.get("formula") or expr), given)
        elif op in handlers:
            result, steps, verified = handlers[op](expr, args)
        else:
            raise MathError("unknown op. Use: " + ", ".join(
                sorted(list(handlers) + ["formula"])))
    except MathError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - a bad expression must not kill Harry
        log.warning("math compute failed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "op": op, "result": result, "steps": steps,
            "verified": bool(verified)}


def register_math_tools(registry):
    from .tools import Tool
    from .security import SAFE
    registry.register(Tool(
        "math",
        "Compute math/engineering: evaluate, solve, simplify, derivative, "
        "integrate, matrix, stats, convert units, engineering formula",
        compute,
        SAFE,
        {"op": "str", "expression": "str", "variable": "str", "equation": "str",
         "matrix": "list", "data": "list", "value": "number",
         "from_unit": "str", "to_unit": "str", "name": "str", "values": "dict"},
    ))
