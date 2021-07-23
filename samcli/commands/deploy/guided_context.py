"""
Class to manage all the prompts during a guided sam deploy
"""

import logging
from typing import Dict, Any, List

import click
from botocore.session import get_session
from click.types import FuncParamType
from click import prompt
from click import confirm

from samcli.commands._utils.options import _space_separated_list_func_type, DEFAULT_STACK_NAME
from samcli.commands.deploy.code_signer_utils import (
    signer_config_per_function,
    extract_profile_name_and_owner_from_existing,
    prompt_profile_name,
    prompt_profile_owner,
)
from samcli.commands.deploy.exceptions import GuidedDeployFailedError
from samcli.commands.deploy.guided_config import GuidedConfig
from samcli.commands.deploy.auth_utils import auth_per_resource
from samcli.commands.deploy.utils import sanitize_parameter_overrides
from samcli.lib.config.samconfig import DEFAULT_ENV, DEFAULT_CONFIG_FILE_NAME
from samcli.lib.bootstrap.bootstrap import manage_stack
from samcli.lib.package.ecr_utils import is_ecr_url
from samcli.lib.package.image_utils import tag_translation, NonLocalImageException, NoImageFoundException
from samcli.lib.providers.provider import Stack
from samcli.lib.providers.sam_function_provider import SamFunctionProvider
from samcli.lib.providers.sam_stack_provider import SamLocalStackProvider
from samcli.lib.utils.colors import Colored
from samcli.lib.utils.packagetype import IMAGE

LOG = logging.getLogger(__name__)


class GuidedContext:
    def __init__(  # pylint: disable=too-many-statements
        self,
        stack_name,
        s3_bucket,
        image_repository,
        image_repositories,
        s3_prefix,
        iac,
        project,
        region=None,
        profile=None,
        confirm_changeset=None,
        capabilities=None,
        signing_profiles=None,
        parameter_overrides=None,
        save_to_config=True,
        config_section=None,
        config_env=None,
        config_file=None,
    ):
        self._iac = iac
        self._project = project
        self.stack_name = stack_name or project.default_stack.name or DEFAULT_STACK_NAME
        self.s3_bucket = s3_bucket
        self.image_repository = image_repository
        self.image_repositories = image_repositories
        self.s3_prefix = s3_prefix
        self.region = region
        self.profile = profile
        self.confirm_changeset = confirm_changeset
        self.capabilities = (capabilities,)
        self.parameter_overrides_from_cmdline = parameter_overrides
        self.save_to_config = save_to_config
        self.config_section = config_section
        self.config_env = config_env
        self.config_file = config_file
        self.guided_stack_name = None
        self.guided_s3_bucket = None
        self.guided_image_repository = None
        self.guided_image_repositories = None
        self.guided_s3_prefix = None
        self.guided_region = None
        self.guided_profile = None
        self.signing_profiles = signing_profiles
        self._capabilities = None
        self._parameter_overrides = None
        self.start_bold = "\033[1m"
        self.end_bold = "\033[0m"
        self.color = Colored()
        self.function_provider = None
        self._iac_stack = None
        self.template_file = None

    @property
    def guided_capabilities(self):
        return self._capabilities

    @property
    def guided_parameter_overrides(self):
        return self._parameter_overrides

    def _get_iac_stack(self, provided_stack_name):
        """
        get iac_stack from project based on stack_name
        """
        stack = self._project.find_stack_by_name(provided_stack_name) or self._project.default_stack
        if stack is None or (stack.name and stack.name != provided_stack_name):
            raise GuidedDeployFailedError(
                f"There is no stack with name '{provided_stack_name}'. "
                "If you have specified --stack-name, specify the correct stack name "
                "or remove --stack-name to use default."
            )

        self._iac_stack = stack
        self.template_file = stack.origin_dir
        self.stack_name = stack.name if stack.name else provided_stack_name

    # pylint: disable=too-many-statements
    def guided_prompts(self):
        """
        Start an interactive cli prompt to collection information for deployment

        """
        default_stack_name = self.stack_name
        default_region = self.region or get_session().get_config_variable("region") or "us-east-1"
        default_capabilities = self.capabilities[0] or ("CAPABILITY_IAM",)
        default_config_env = self.config_env or DEFAULT_ENV
        default_config_file = self.config_file or DEFAULT_CONFIG_FILE_NAME
        input_capabilities = None
        config_env = None
        config_file = None

        click.echo(
            self.color.yellow(
                "\n\tSetting default arguments for 'sam deploy'\n\t========================================="
            )
        )

        stack_name = prompt(
            f"\t{self.start_bold}Stack Name{self.end_bold}", default=default_stack_name, type=click.STRING
        )
        self._get_iac_stack(stack_name)
        region = prompt(f"\t{self.start_bold}AWS Region{self.end_bold}", default=default_region, type=click.STRING)
        parameter_override_keys = self._iac_stack.get_overrideable_parameters()
        input_parameter_overrides = self.prompt_parameters(
            parameter_override_keys, self.parameter_overrides_from_cmdline, self.start_bold, self.end_bold
        )
        stacks, _ = SamLocalStackProvider.get_stacks(
            [self._iac_stack], parameter_overrides=sanitize_parameter_overrides(input_parameter_overrides)
        )
        image_repositories = self.prompt_image_repository(stacks)

        click.secho("\t#Shows you resources changes to be deployed and require a 'Y' to initiate deploy")
        confirm_changeset = confirm(
            f"\t{self.start_bold}Confirm changes before deploy{self.end_bold}", default=self.confirm_changeset
        )
        click.secho("\t#SAM needs permission to be able to create roles to connect to the resources in your template")
        capabilities_confirm = confirm(
            f"\t{self.start_bold}Allow SAM CLI IAM role creation{self.end_bold}", default=True
        )

        if not capabilities_confirm:
            input_capabilities = prompt(
                f"\t{self.start_bold}Capabilities{self.end_bold}",
                default=list(default_capabilities),
                type=FuncParamType(func=_space_separated_list_func_type),
            )

        self.prompt_authorization(stacks)
        self.prompt_code_signing_settings(stacks)

        save_to_config = confirm(
            f"\t{self.start_bold}Save arguments to configuration file{self.end_bold}", default=True
        )
        if save_to_config:
            config_file = prompt(
                f"\t{self.start_bold}SAM configuration file{self.end_bold}",
                default=default_config_file,
                type=click.STRING,
            )
            config_env = prompt(
                f"\t{self.start_bold}SAM configuration environment{self.end_bold}",
                default=default_config_env,
                type=click.STRING,
            )

        s3_bucket = manage_stack(profile=self.profile, region=region)
        click.echo(f"\n\t\tManaged S3 bucket: {s3_bucket}")
        click.echo("\t\tA different default S3 bucket can be set in samconfig.toml")

        self.guided_stack_name = stack_name
        self.guided_s3_bucket = s3_bucket
        self.guided_image_repositories = image_repositories
        self.guided_s3_prefix = stack_name
        self.guided_region = region
        self.guided_profile = self.profile
        self._capabilities = input_capabilities if input_capabilities else default_capabilities
        self._parameter_overrides = (
            input_parameter_overrides if input_parameter_overrides else self.parameter_overrides_from_cmdline
        )
        self.save_to_config = save_to_config
        self.config_env = config_env if config_env else default_config_env
        self.config_file = config_file if config_file else default_config_file
        self.confirm_changeset = confirm_changeset

    def prompt_authorization(self, stacks: List[Stack]):
        auth_required_per_resource = auth_per_resource(stacks)

        for resource, authorization_required in auth_required_per_resource:
            if not authorization_required:
                auth_confirm = confirm(
                    f"\t{self.start_bold}{resource} may not have authorization defined, Is this okay?{self.end_bold}",
                    default=False,
                )
                if not auth_confirm:
                    raise GuidedDeployFailedError(msg="Security Constraints Not Satisfied!")

    def prompt_code_signing_settings(self, stacks: List[Stack]):
        """
        Prompt code signing settings to ask whether customers want to code sign their code and
        display signing details.

        Parameters
        ----------
        stacks : List[Stack]
            List of stacks to search functions and layers
        """
        (functions_with_code_sign, layers_with_code_sign) = signer_config_per_function(stacks)

        # if no function or layer definition found with code signing, skip it
        if not functions_with_code_sign and not layers_with_code_sign:
            LOG.debug("No function or layer definition found with code sign config, skipping")
            return

        click.echo("\n\t#Found code signing configurations in your function definitions")
        sign_functions = confirm(
            f"\t{self.start_bold}Do you want to sign your code?{self.end_bold}",
            default=True,
        )

        if not sign_functions:
            LOG.debug("User skipped code signing, continuing rest of the process")
            self.signing_profiles = None
            return

        if not self.signing_profiles:
            self.signing_profiles = {}

        click.echo("\t#Please provide signing profile details for the following functions & layers")

        for function_name in functions_with_code_sign:
            (profile_name, profile_owner) = extract_profile_name_and_owner_from_existing(
                function_name, self.signing_profiles
            )

            click.echo(f"\t#Signing profile details for function '{function_name}'")
            profile_name = prompt_profile_name(profile_name, self.start_bold, self.end_bold)
            profile_owner = prompt_profile_owner(profile_owner, self.start_bold, self.end_bold)
            self.signing_profiles[function_name] = {"profile_name": profile_name, "profile_owner": profile_owner}
            self.signing_profiles[function_name]["profile_owner"] = "" if not profile_owner else profile_owner

        for layer_name, functions_use_this_layer in layers_with_code_sign.items():
            (profile_name, profile_owner) = extract_profile_name_and_owner_from_existing(
                layer_name, self.signing_profiles
            )
            click.echo(
                f"\t#Signing profile details for layer '{layer_name}', "
                f"which is used by functions {functions_use_this_layer}"
            )
            profile_name = prompt_profile_name(profile_name, self.start_bold, self.end_bold)
            profile_owner = prompt_profile_owner(profile_owner, self.start_bold, self.end_bold)
            self.signing_profiles[layer_name] = {"profile_name": profile_name, "profile_owner": profile_owner}
            self.signing_profiles[layer_name]["profile_owner"] = "" if not profile_owner else profile_owner

        LOG.debug("Signing profile names and owners %s", self.signing_profiles)

    def prompt_parameters(
        self, parameter_override_from_template, parameter_override_from_cmdline, start_bold, end_bold
    ):
        _prompted_param_overrides = {}
        if parameter_override_from_template:
            for parameter_key, parameter_properties in parameter_override_from_template.items():
                no_echo = parameter_properties.get("NoEcho", False)
                if no_echo:
                    parameter = prompt(
                        f"\t{start_bold}Parameter {parameter_key}{end_bold}", type=click.STRING, hide_input=True
                    )
                    _prompted_param_overrides[parameter_key] = {"Value": parameter, "Hidden": True}
                else:
                    parameter = prompt(
                        f"\t{start_bold}Parameter {parameter_key}{end_bold}",
                        default=_prompted_param_overrides.get(
                            parameter_key,
                            self._get_parameter_value(
                                parameter_key, parameter_properties, parameter_override_from_cmdline
                            ),
                        ),
                        type=click.STRING,
                    )
                    _prompted_param_overrides[parameter_key] = {"Value": parameter, "Hidden": False}
        return _prompted_param_overrides

    def prompt_image_repository(self, stacks: List[Stack]):
        """
        Prompt for the image repository to push the images.
        For each image function found in build artifacts, it will prompt for an image repository.

        Parameters
        ----------
        stacks : List[Stack]
            List of stacks to look for image functions.

        Returns
        -------
        Dict
            A dictionary contains image function logical ID as key, image repository as value.
        """
        image_repositories = {}
        if self._iac_stack.has_assets_of_package_type(IMAGE):
            self.function_provider = SamFunctionProvider(stacks, ignore_code_extraction_warnings=True)
            function_resources = [
                resource.item_id for resource in self._iac_stack.find_function_resources_of_package_type(IMAGE)
            ]
            for resource_id in function_resources:
                image_repositories[resource_id] = prompt(
                    f"\t{self.start_bold}Image Repository for {resource_id}{self.end_bold}",
                    default=self.image_repositories.get(resource_id, "")
                    if isinstance(self.image_repositories, dict)
                    else "" or self.image_repository,
                )
                if not is_ecr_url(image_repositories.get(resource_id)):
                    raise GuidedDeployFailedError(
                        f"Invalid Image Repository ECR URI: {image_repositories.get(resource_id)}"
                    )
            for resource_id, function_prop in self.function_provider.functions.items():
                if function_prop.packagetype == IMAGE:
                    image = function_prop.imageuri
                    try:
                        tag = tag_translation(image)
                    except NonLocalImageException:
                        pass
                    except NoImageFoundException as ex:
                        raise GuidedDeployFailedError("No images found to deploy, try running sam build") from ex
                    else:
                        click.secho(f"\t  {image} to be pushed to {image_repositories.get(resource_id)}:{tag}")
            click.secho(nl=True)

        return image_repositories

    def run(self):

        self.guided_prompts()

        guided_config = GuidedConfig(template_file=self.template_file, section=self.config_section)
        guided_config.read_config_showcase(
            self.config_file or DEFAULT_CONFIG_FILE_NAME,
        )

        if self.save_to_config:
            guided_config.save_config(
                self._parameter_overrides,
                self.config_env or DEFAULT_ENV,
                self.config_file or DEFAULT_CONFIG_FILE_NAME,
                stack_name=self.guided_stack_name,
                s3_bucket=self.guided_s3_bucket,
                s3_prefix=self.guided_s3_prefix,
                image_repositories=self.guided_image_repositories,
                region=self.guided_region,
                profile=self.guided_profile,
                confirm_changeset=self.confirm_changeset,
                capabilities=self._capabilities,
                signing_profiles=self.signing_profiles,
            )

    @staticmethod
    def _get_parameter_value(
        parameter_key: str, parameter_properties: Dict, parameter_override_from_cmdline: Dict
    ) -> Any:
        """
        This function provide the value of a parameter. If the command line/config file have "override_parameter"
        whose key exist in the template file parameters, it will use the corresponding value.
        Otherwise, it will use its default value in template file.

        :param parameter_key: key of parameter
        :param parameter_properties: properties of that parameters from template file
        :param parameter_override_from_cmdline: parameter_override from command line/config file
        """
        if parameter_override_from_cmdline and parameter_override_from_cmdline.get(parameter_key, None):
            return parameter_override_from_cmdline[parameter_key]
        # Make sure the default is casted to a string.
        return str(parameter_properties.get("Default", ""))
