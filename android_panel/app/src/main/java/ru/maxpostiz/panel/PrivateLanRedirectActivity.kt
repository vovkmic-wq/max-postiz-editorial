package ru.maxpostiz.panel

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import net.openid.appauth.AuthorizationManagementActivity

class PrivateLanRedirectActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val expected = Uri.parse(BuildConfig.REDIRECT_URI)
        val callback = intent.data
        if (BuildConfig.PRIVATE_LAN && intent.action == Intent.ACTION_VIEW && callback != null &&
            callback.scheme == expected.scheme && callback.encodedAuthority == expected.encodedAuthority &&
            callback.encodedPath == expected.encodedPath && callback.fragment == null &&
            !callback.encodedQuery.isNullOrEmpty()) {
            startActivity(AuthorizationManagementActivity.createResponseHandlingIntent(this, callback))
        }
        finish()
    }
}
