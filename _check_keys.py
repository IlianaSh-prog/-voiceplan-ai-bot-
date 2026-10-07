# -*- coding: utf-8 -*-
"""Проверка языковых таблиц bot.py: одинаковый набор ключей RU/EN и плейсхолдеры."""
import ast
import re
import sys

SRC = "bot.py"

with open(SRC, encoding="utf-8") as fh:
    tree = ast.parse(fh.read())

langs_node = None
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "LANGS":
                langs_node = node
if langs_node is None:
    print("LANGS not found")
    sys.exit(1)

langs = ast.literal_eval(langs_node.value)
ru, en = set(langs["ru"]), set(langs["en"])

problems = []

only_ru = sorted(ru - en)
only_en = sorted(en - ru)
if only_ru:
    problems.append("Keys only in RU: " + ", ".join(only_ru))
if only_en:
    problems.append("Keys only in EN: " + ", ".join(only_en))

placeholder = re.compile(r"{(\w+)}")
for key in sorted(ru & en):
    ph_ru = set(placeholder.findall(langs["ru"][key]))
    ph_en = set(placeholder.findall(langs["en"][key]))
    if ph_ru != ph_en:
        problems.append(
            f"Placeholders differ for '{key}': RU={sorted(ph_ru)} EN={sorted(ph_en)}"
        )

if problems:
    print("FAIL")
    for p in problems:
        print(" -", p)
    sys.exit(1)

print(f"OK: {len(ru)} keys, RU=EN, placeholders match")
