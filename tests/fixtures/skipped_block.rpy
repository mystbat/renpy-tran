# game script sample: a non-translatable block (preserved verbatim + skipped)
init python:
    def helper():
        return "this code must not be touched"

screen confirm():
    text "This is a UI screen, skipped by parser."

transform fade_in:
    alpha 0.0
    linear 0.5 alpha 1.0
