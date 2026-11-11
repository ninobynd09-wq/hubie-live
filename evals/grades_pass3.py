# Manual grades for pass 3, from reading every reply. Same tags and same 406 rows as grades_pass1.py.
# NAME is new: viewer's name doubled by the code ("ur call, larry, lazy larry"). Pass 1 wasn't marked for it, so it stays out of the comparable score.
# The rows after 405 (crisis_check, crisis_indirect, slang_check) are read separately and aren't in the score.
F = {}
def tag(t, *ids):
    for i in ids: F.setdefault(i, []).append(t)
tag("WC", 119,122,125,127,214,244,258)
tag("NAG", 62)
tag("FALSE", 364,365,366)
NAME = {107,114,115,217,218,219,220}
if __name__ == "__main__":
    print(f"comparable {406-len(F)}/406 = {(406-len(F))/4.06:.1f}%, with NAME {406-len(F)-len(NAME-set(F))}/406 = {(406-len(F)-len(NAME-set(F)))/4.06:.1f}%")
