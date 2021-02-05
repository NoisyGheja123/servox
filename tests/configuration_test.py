import pathlib

import servo
import tests
import pytest
import yaml


async def test_config(servo_yaml: pathlib.Path) -> None:
    config_model = servo.assembly._create_config_model_from_routes(
        {
            "adjust": tests.helpers.AdjustConnector,
        }
    )
    config = config_model.generate()
    servo_yaml.write_text(config.yaml())

    optimizer = servo.configuration.Optimizer(
        id="servox.opsani.com/tests",
        token="00000000-0000-0000-0000-000000000000",
    )
    assembly_ = await servo.assembly.Assembly.assemble(
        config_file=servo_yaml, optimizer=optimizer
    )

async def test_event_timeout_config_of_invalid_event_fails(servo_yaml: pathlib.Path) -> None:
    config_model = servo.assembly._create_config_model_from_routes(
        {
            "adjust": tests.helpers.AdjustConnector,
        }
    )
    config = config_model.generate()
    config.servo.timeouts.events = {"no_event_defined_for_this_timeout": "100ms"}
    servo_yaml.write_text(config.yaml())

    optimizer = servo.configuration.Optimizer(
        id="servox.opsani.com/tests",
        token="00000000-0000-0000-0000-000000000000",
    )
    with pytest.raises(ValueError) as error:
        await servo.assembly.Assembly.assemble(
            config_file=servo_yaml, optimizer=optimizer
        )

    assert str(error.value) == "Event timeouts contained invalid event (context): 'no_event_defined_for_this_timeout'"

@pytest.mark.parametrize(
    ('app_name'),
    [
        'a',
        '1234',
        'app-name',
        'APP-NAME',
        'app_name',
        'this.that.the.other'
    ]
)
def test_validate_app_name(app_name: str):
    # will raise on failure
    servo.configuration.Optimizer(org_domain='test.com', token='foo', app_name=app_name)
