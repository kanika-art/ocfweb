from typing import Any
from typing import Union

import ocflib.account.search as search
import ocflib.account.validators as validators
import ocflib.misc.validators
import ocflib.ucb.directory as directory
from Crypto.PublicKey import RSA
from django import forms
from django.http import HttpRequest
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseRedirect
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from ocflib.account.creation import CREATE_PUBLIC_KEY
from ocflib.account.creation import encrypt_password
from ocflib.account.creation import NewAccountRequest
from ocflib.account.creation import validate_username
from ocflib.account.creation import ValidationError
from ocflib.account.creation import ValidationWarning
from ocflib.account.search import user_attrs_ucb
from ocflib.account.submission import NewAccountResponse
from ocflib.ucb.groups import group_by_oid
from ocflib.ucb.groups import groups_by_student_signat

import ocfweb.account.recommender as recommender
from ocfweb.account.constants import TEST_GROUP_ACCOUNTS
from ocfweb.account.constants import TESTER_CALNET_UIDS
from ocfweb.auth import calnet_required
from ocfweb.component.celery import celery_app
from ocfweb.component.celery import validate_then_create_account
from ocfweb.component.forms import Form
from ocfweb.component.forms import wrap_validator


@calnet_required
def request_account(request: HttpRequest) -> Union[HttpResponseRedirect, HttpResponse]:
    calnet_uid = request.session['calnet_uid']
    status = 'new_request'

    existing_accounts = search.users_by_calnet_uid(calnet_uid)
    groups_for_user = groups_by_student_signat(calnet_uid)

    eligible_new_group_accounts, existing_group_accounts = {}, {}
    for group_oid in groups_for_user:
        if not group_by_oid(group_oid)['accounts'] or group_oid in [group[0] for group in TEST_GROUP_ACCOUNTS]:
            eligible_new_group_accounts[group_oid] = groups_for_user[group_oid]
        else:
            existing_group_accounts[group_oid] = groups_for_user[group_oid]

    if existing_accounts and not eligible_new_group_accounts and calnet_uid not in TESTER_CALNET_UIDS:
        return render(
            request,
            'account/register/already-has-account.html',
            {
                'account': ', '.join(existing_accounts),
                'calnet_uid': calnet_uid,
                'title': 'You already have an account',
            },
        )

    # ensure we can even find them in university LDAP
    # (alumni etc. might not be readable in LDAP but can still auth via CalNet)
    if not user_attrs_ucb(calnet_uid):
        return render(
            request,
            'account/register/cant-find-in-ldap.html',
            {
                'calnet_uid': calnet_uid,
                'title': 'Unable to read account information',
            },
        )

    real_name = directory.name_by_calnet_uid(calnet_uid)

    association_choices = []
    if not existing_accounts or calnet_uid in TESTER_CALNET_UIDS:
        association_choices.append((calnet_uid, real_name))
    for group_oid, group in eligible_new_group_accounts.items():
        association_choices.append((group_oid, group['name']))

    if request.method == 'POST':
        form = ApproveForm(request.POST, association_choices=association_choices)
        if form.is_valid():
            assoc_id = int(form.cleaned_data['account_association'])
            is_group_account = assoc_id != calnet_uid
            if is_group_account:
                req = NewAccountRequest(
                    user_name=form.cleaned_data['ocf_login_name'],
                    real_name=eligible_new_group_accounts[assoc_id]['name'],
                    is_group=True,
                    calnet_uid=None,
                    callink_oid=assoc_id,
                    email=form.cleaned_data['contact_email'],
                    encrypted_password=encrypt_password(
                        form.cleaned_data['password'],
                        RSA.importKey(CREATE_PUBLIC_KEY),
                    ),
                    handle_warnings=NewAccountRequest.WARNINGS_WARN,
                )
            else:
                req = NewAccountRequest(
                    user_name=form.cleaned_data['ocf_login_name'],
                    real_name=real_name,
                    is_group=False,
                    calnet_uid=calnet_uid,
                    callink_oid=None,
                    email=form.cleaned_data['contact_email'],
                    encrypted_password=encrypt_password(
                        form.cleaned_data['password'],
                        RSA.importKey(CREATE_PUBLIC_KEY),
                    ),
                    handle_warnings=NewAccountRequest.WARNINGS_WARN,
                )
            if 'warnings-submit' in request.POST:
                req = req._replace(
                    handle_warnings=NewAccountRequest.WARNINGS_SUBMIT,
                )

            task = validate_then_create_account.delay(req)
            task.wait(timeout=5)

            if isinstance(task.result, NewAccountResponse):
                if task.result.status == NewAccountResponse.REJECTED:
                    status = 'has_errors'
                    form.add_error(None, task.result.errors)
                elif task.result.status == NewAccountResponse.FLAGGED:
                    status = 'has_warnings'
                    form.add_error(None, task.result.errors)
                elif task.result.status == NewAccountResponse.PENDING:
                    return HttpResponseRedirect(reverse('account_pending'))
                else:
                    raise AssertionError('Unexpected state reached')
            else:
                # validation was successful, the account is being created now
                request.session['approve_task_id'] = task.result
                return HttpResponseRedirect(reverse('wait_for_account'))
    else:
        form = ApproveForm(association_choices=association_choices)

    return render(
        request,
        'account/register/index.html',
        {
            'calnet_uid': calnet_uid,
            'existing_accounts': existing_accounts,
            'existing_group_accounts': existing_group_accounts,
            'form': form,
            'real_name': real_name,
            'status': status,
            'title': 'Request an OCF account',
        },
    )


def recommend(request: HttpRequest) -> Union[JsonResponse, HttpResponseBadRequest]:
    real_name = request.GET.get('real_name', None)
    if real_name is None:
        return HttpResponseBadRequest('No real_name in recommend request')

    recommendations = recommender.recommend(real_name, 10)
    return JsonResponse(
        {
            'recommendations': recommendations,
        },
    )


def validate(request: HttpRequest) -> Union[HttpResponseBadRequest, JsonResponse]:
    real_name = request.GET.get('real_name', None)
    if real_name is None:
        return HttpResponseBadRequest('No real_name in validate request')
    username = request.GET.get('username', None)
    if username is None:
        return HttpResponseBadRequest('No username in validate request')

    try:
        validate_username(username, real_name)
        return JsonResponse({
            'is_valid': True,
            'msg': 'Username is available.',
        })
    except (ValidationError, ValidationWarning) as e:
        return JsonResponse({
            'is_valid': False,
            'is_warning': isinstance(e, ValidationWarning),
            'msg': str(e),
        })


def wait_for_account(request: HttpRequest) -> Union[HttpResponse, HttpResponseRedirect]:
    if 'approve_task_id' not in request.session:
        return render(
            request,
            'account/register/wait/error-no-task-id.html',
            {'title': 'Account request error'},
        )

    task = celery_app.AsyncResult(request.session['approve_task_id'])
    if not task.ready():
        meta = task.info
        status = ['Starting creation']
        if isinstance(meta, dict) and 'status' in meta:
            status.extend(meta['status'])
        return render(
            request,
            'account/register/wait/wait.html',
            {
                'title': 'Creating account...',
                'status': status,
            },
        )
    elif isinstance(task.result, NewAccountResponse):
        if task.result.status == NewAccountResponse.CREATED:
            return HttpResponseRedirect(reverse('account_created'))
    elif isinstance(task.result, Exception):
        raise task.result

    return render(request, 'account/register/wait/error-probably-not-created.html', {})


def account_pending(request: HttpRequest) -> HttpResponse:
    return render(request, 'account/register/pending.html', {'title': 'Account request pending'})


def account_created(request: HttpRequest) -> HttpResponse:
    return render(request, 'account/register/success.html', {'title': 'Account request successful'})


class ApproveForm(Form):

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        association_choices = kwargs.pop('association_choices', None)
        super().__init__(*args, **kwargs)
        if association_choices:
            self.fields['account_association'].choices = association_choices

    account_association = forms.ChoiceField(
        label='CalNet/CalLink account association',
        widget=forms.Select(),
    )

    ocf_login_name = forms.CharField(
        label='OCF account name',
        widget=forms.TextInput(attrs={'placeholder': 'jsmith'}),
        validators=[wrap_validator(validators.validate_username)],
        min_length=3,
        max_length=16,
    )

    # password is validated in clean since we need the username as part of the
    # password validation (to compare similarity)
    password = forms.CharField(
        widget=forms.PasswordInput(render_value=True),
        label='Password',
        min_length=8,
        max_length=256,
    )

    verify_password = forms.CharField(
        widget=forms.PasswordInput(render_value=True),
        label='Confirm password',
        min_length=8,
        max_length=64,
    )

    contact_email = forms.EmailField(
        label='Contact e-mail',
        validators=[wrap_validator(ocflib.misc.validators.valid_email)],
        widget=forms.EmailInput(attrs={'placeholder': 'jsmith@berkeley.edu'}),
    )

    verify_contact_email = forms.EmailField(
        label='Confirm contact e-mail',
        widget=forms.EmailInput(attrs={'placeholder': 'jsmith@berkeley.edu'}),
    )

    disclaimer_agreement = forms.BooleanField(
        label='I agree with the above statement.',
        error_messages={
            'required': 'You must agree to our policies.',
        },
    )

    def clean_verify_password(self) -> str:
        password = self.cleaned_data.get('password')
        verify_password = self.cleaned_data.get('verify_password')

        if password and verify_password:
            if password != verify_password:
                raise forms.ValidationError("Your passwords don't match.")
        return verify_password

    def clean_verify_contact_email(self) -> str:
        email = self.cleaned_data.get('contact_email')
        verify_contact_email = self.cleaned_data.get('verify_contact_email')

        if email and verify_contact_email:
            if email != verify_contact_email:
                raise forms.ValidationError("Your emails don't match.")
        return verify_contact_email

    # clean incompatible with supertype BaseForm which is defined in django.
    def clean(self) -> None:  # type: ignore
        cleaned_data = super().clean()

        # validate password (requires username to check similarity)
        username = cleaned_data.get('username')
        password = cleaned_data.get('password')

        if username and password:
            wrap_validator(validators.validate_password)(username, password)
