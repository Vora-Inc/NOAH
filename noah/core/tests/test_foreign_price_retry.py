"""An unavailable exact-block price keeps its original evaluation pending."""
from noah.chain.foreign_value import ForeignDepositUnpriced
from noah.core.loop import read_state, run_forever
from noah.core.tests.test_loop import DAY_ONE_LAST_CHANCE_HEIGHT, DayAwareChain


class PriceUnavailableChain(DayAwareChain):
    unavailable = True

    def read(self, height):
        result = super().read(height)
        if self.unavailable:
            error = ForeignDepositUnpriced("exact receipt block price unavailable")
            error.day = result["day"]
            raise error
        return result


def run(chain, tmp_path, iterations=1, sleep=lambda _: None):
    outcomes = []
    run_forever(chain, settle_blocks=chain.w.get("settle_blocks", 1),
                state_path=str(tmp_path / "loop.json"),
                lock_path=str(tmp_path / "lock"), poll_seconds=1,
                sleep=sleep, iterations=iterations, on_tick=outcomes.append)
    return outcomes, read_state(tmp_path / "loop.json")


def test_price_failure_during_catchup_survives_restart_and_publishes_once(tmp_path):
    chain = PriceUnavailableChain(head=400, settle_blocks=1)
    chain.published_days.add(0)
    outcomes, state = run(chain, tmp_path, 2, lambda _: setattr(chain, "head", 720))
    assert outcomes[1].outcome == "day_catchup_pending"
    assert "exact-block foreign price" in outcomes[1].detail
    assert state["day_catchup_pending"] == {"day": 1, "height": DAY_ONE_LAST_CHANCE_HEIGHT}
    assert DAY_ONE_LAST_CHANCE_HEIGHT not in state.get("refused_heights", [])
    assert chain.publish_calls == []
    chain.unavailable = False
    chain.head = 1080
    outcomes, state = run(chain, tmp_path)
    assert outcomes[0].outcome == "published"
    assert len(chain.publish_calls) == 1
    assert chain.publish_calls[0][2] == DAY_ONE_LAST_CHANCE_HEIGHT
    run(chain, tmp_path)
    assert len([call for call in chain.publish_calls if call[2] == DAY_ONE_LAST_CHANCE_HEIGHT]) == 1


def test_initial_closed_day_price_failure_is_persisted_for_retry(tmp_path):
    chain = PriceUnavailableChain(head=3000, settle_blocks=800)
    chain.published_days.update(range(5))
    outcomes, state = run(chain, tmp_path)
    assert outcomes[0].outcome == "refused"
    assert state["day_catchup_pending"] == {"day": 5, "height": 1799}
    assert 1799 not in state.get("refused_heights", [])
    assert chain.publish_calls == []
