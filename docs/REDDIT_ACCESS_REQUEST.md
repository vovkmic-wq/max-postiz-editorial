# Черновик запроса доступа к Reddit для перевода публикаций

Этот текст предназначен для официальной формы доступа. Заявка не отправлена;
перед отправкой владелец указывает Reddit username и действительный статус
монетизации, затем проверяет условия, которые готов выполнять. Пароль
аккаунта, client secret и токены в заявку и этот файл не включаются.

## Subject

Request for approved Reddit data access and translated distribution in a MAX channel

## Project description

I am planning a self-hosted editorial service for a Russian-language MAX channel.
It would select up to ten suitable English-language Reddit posts every four hours,
with a maximum of sixty separate publications per day. The intended topics are
interesting stories, science and humor. Political and NSFW content would be excluded.

I am requesting guidance and explicit authorization for this use case, including
translation and distribution outside Reddit. I understand that standard API access
does not itself establish the right to modify or redistribute users' content.
I would use only content and rights that you and the relevant rights holders permit.

The proposed processing would use local language models for translation and quality
checking, not model training. Posts would link to their original Reddit sources
and would be identified as machine-assisted translations. The intended access is
read-only; the service would not vote, comment, message users or act as a moderation tool.

The service is not deployed for Reddit collection yet, and no approved OAuth app
has been created for this project. Please advise on the appropriate registration
and access route, and whether this external workflow can be approved under your
current rules or requires a separate agreement. The processing and destination
are outside Reddit, rather than an app running only within Reddit.

Before deployment, I would implement the required access controls, rate-limit
handling, retention limits and removal workflows. The existing MAX delivery
adapter does not yet automate deletion of published messages; that capability
or an acceptable removal process must be resolved before enabling distribution.
I would not claim that it is already implemented.

## Fields for the owner

- Reddit username: enter the actual username, not the email address.
- Project or source URL: provide a real accessible reference if requested.
- Monetization: state the actual status; do not assume that a personal account
  makes every future use non-commercial.
- Requested retention/removal handling: confirm the approved requirements
  and implementation before enabling the collector.

## Official submission route

[Reddit access request form](https://support.reddithelp.com/hc/en-us/requests/new?tf_42139884615700=api_request_type_developer_clone&ticket_form_id=14868593862164)

[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy),
[Data API Terms](https://redditinc.com/policies/data-api-terms).
