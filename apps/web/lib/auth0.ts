import { Auth0Client } from "@auth0/nextjs-auth0/server";

import { authorizationParametersFrom } from "./auth0-config";

export const auth0 = new Auth0Client({
  authorizationParameters: authorizationParametersFrom(process.env),
  enableAccessTokenEndpoint: false,
  enableConnectAccountEndpoint: false,
  tokenRefreshBuffer: 60,
});
