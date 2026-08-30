# CVXPY Battery Optimizer Plan

Treat this as a **fresh receding-horizon optimizer**, not a rewrite of the old
rule-based battery helper. The first step is to define the **new objective
function** clearly: what is being optimized, what is a hard constraint, and
what is only a soft penalty. That objective becomes the center of the CVXPY
model; everything else is just inputs and constraints.

1. **Freeze the new optimization target spec first.** Decide whether the solver
   should minimize net cost, maximize self-sufficiency, maximize export value,
   minimize battery wear, reduce mode switching, or some weighted mix. Put
   those terms behind explicit weights or priorities so the target can evolve
   without rewriting the sensor.

2. **Move the math into a pure optimizer module.** Create a helper like
   `solve_battery_schedule(...) -> plan` that takes arrays for price, solar
   forecast, battery state, and limits, and returns the full slot-by-slot mode
   schedule plus solver status. Keep Home Assistant code out of the solver so
   it stays testable and can run in an executor.

3. **Model the battery as a quarter-hour CVXPY problem.** Use one binary mode
   variable per slot if you want exact discrete modes, or continuous
   charge/discharge variables plus a projection step if you need something
   lighter. Add SoC dynamics, battery power limits, efficiency, reserve floor,
   and any grid import/export constraints you need. If the horizon is long,
   solve the longest common 15-minute horizon available from the price and solar
   inputs; where one forecast ends earlier, use conservative terminal
   assumptions so you still plan as far ahead as possible.

4. **Make it a receding-horizon controller.** Recompute the full future plan
   whenever the price sensor updates, the solar forecast updates, or the
   battery state changes. In between those updates, run a local 15-minute tick
   so the active slot advances even if no new market data arrives. Each tick
   should shift the current slot forward and re-evaluate the plan from the new
   present.

5. **Keep the sensor thin.** The sensor should expose the current mode as
   state, and the full solved schedule plus solver diagnostics as attributes.
   Good attributes are `schedule`, `horizon_start`, `horizon_end`,
   `objective_value`, `solver_status`, `reason`, and `next_mode_change`. If the
   solver fails, do not invent a new plan silently; keep the last valid plan and
   surface the failure state explicitly.

6. **Run CVXPY off the event loop.** Solving should happen in a background
   executor because it is CPU-bound. Cache the latest solution, and only
   re-solve when inputs or the quarter-hour boundary actually require it.

7. **Test the optimizer before wiring the UI.** Add synthetic scenario tests
   for cheap-night/high-solar, expensive-night/no-solar, truncated forecast
   horizons, infeasible constraints, and quarter-hour rollover. Also add one or
   two golden tests that verify the returned mode schedule is stable for a known
   input set.

If you want the shortest implementation path, build it in this order:
**objective spec → pure solver module → sensor adapter → 15-minute receding
horizon → tests**. That gives you a clean CVXPY core and keeps the Home
Assistant entity mostly as a presentation layer.
