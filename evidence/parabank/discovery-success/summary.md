# Find the total money in all accounts

Completed after 5 steps, reading total account balance = '$515.50'.

- run: `20260909T180920Z-16f1`
- target: https://parabank.parasoft.com/parabank/index.htm?ConnType=JDBC
- took: 23s

## What it did

Numbered as in `trace.jsonl`, so a line here and a line there are the same step.

0. typed 'dummy' into 'Username field' -- ok
1. typed (not recorded) into 'Password textbox' -- ok
2. clicked 'Log In button' -- ok
3. read 'total account balance' -- read `$515.50`
4. declared the goal reached -- ok

## What it produced

- **total account balance** = `$515.50`

## Why it stopped

The planner declared the goal reached and the check it named held. A capability was compiled from this run.
