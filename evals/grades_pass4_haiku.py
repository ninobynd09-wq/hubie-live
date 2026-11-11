# Manual grades for the Haiku pass, from reading every reply. Same tags and same 406 rows as pass 1 and pass 3.
# NAME stays out of the comparable score, as in pass 3. Rows after 405 are read separately.
F = {}
def tag(t, *ids):
    for i in ids: F.setdefault(i, []).append(t)
tag("WC", 11,32,85,86,96,101,164,246,257,288,305,320,326,352,359,392,397)
tag("FALSE", 365,366,368,369)
tag("RCPT", 379,381,382,383,384,385,386,387,391)
tag("UNSAFE", 81,84)
NAME = {217,219}
if __name__ == "__main__":
    print(f"comparable {406-len(F)}/406 = {(406-len(F))/4.06:.1f}%, with NAME {406-len(F)-len(NAME-set(F))}/406 = {(406-len(F)-len(NAME-set(F)))/4.06:.1f}%")
