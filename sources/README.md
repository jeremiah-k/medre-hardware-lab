# Exact Docker source checkouts

`medre-lab sources fetch` provisions independent detached clones for the five
entries in `sources.lock.json`. The lock is the plan, and observed HEADs must
match it. Existing checkouts are never reset or cleaned. Checkouts that an
image bake installs from (every locked source today) are materialized
unfiltered at fetch time, so a cold build can serve `git+file` installs from
them. These are owned lab
inputs, not temporary worktrees; do not sweep them while containers mount them.

Candidate qualification can select different exact commits by editing the lock.
The generated environment points both the core bake and mounted core at the
same selection. Missing or dirty tracked sources fail before build. Generated
checkouts and local environment are ignored by Git.
